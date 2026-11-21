# Market Intelligence — агентная сеть AMFE

Статус: **RESEARCH_ONLY, admission=false**. Это сеть сбора и проверки рыночных фактов, а не
прогнозный модуль. Дата: 2026-11-21. Автор: роль `dense-vintages`.
Машиночитаемая схема: [`org.json`](org.json). Процесс и роли описаны в
[docs/212](../../docs/212_Агентная_сеть_market_intelligence.md); база знаний — в
[docs/213](../../docs/213_База_знаний.md).

## 1. Зачем

Вопросы клиента («что будет с бюджетом при шоке цены», «где TCO хуже», «что меняет логистика»)
нельзя закрывать пересказом по памяти. Нужна сеть, которая:

1. режет вопрос на проверяемые подвопросы;
2. планирует источники **до** сбора;
3. собирает только законно;
4. ставит grade и статус каждой строке;
5. пишет кейс только из проверенного;
6. проверяет независимо;
7. складывает результат в базу знаний с лицензией и сроком свежести.

## 2. Границы (жёстко, не «по умолчанию»)

| Тема | Правило |
|---|---|
| Источники | только открытые публикации, публичная отчётность/раскрытие, регуляторная статистика, лицензированные БД с доступом |
| Закрытые базы | только подписка или письменное разрешение; ключи/доступы не хранятся в репозитории |
| OSINT | открытые страницы и публичные реестры; без взлома, без обхода авторизации/paywall/robots/terms/лимитов |
| Персональные данные | не собираются и не хранятся; только корпоративные роли, контакты — через человека |
| Добросовестность | без подкупа, без оплаты за конфиденциальную информацию, без недобросовестной конкуренции |
| Санкции | комплаенс до касания; ограничение фиксируется, а не обходится |
| Сеть в среде | отсутствует: адаптеры описываются, вызовы не выполняются |
| Выдумывание | нет данных → `NOT_FETCHED`; оценка → `HYPOTHESIS`; цифра без источника запрещена |
| Код | не обходит авторизацию, платный доступ и технические ограничения |
| Публикация | ничего не отправляется и не публикуется агентом |

Детали: [`playbooks/lawful_osint.md`](playbooks/lawful_osint.md),
[`playbooks/red_lines.md`](playbooks/red_lines.md).

## 3. Организационная схема (Lead + 10 ролей)

```
                         ┌───────────────────────────────┐
                         │ director (Director, MI)       │  вопрос, source plan, ceiling
                         └───────────────┬───────────────┘
        ┌────────────────────────────────┼────────────────────────────────┐
        │                                │                                │
┌───────▼────────┐              ┌────────▼────────┐              ┌────────▼─────────┐
│ СЕГМЕНТЫ       │              │ СБОР            │              │ КОНТРОЛЬ/ВЫХОД   │
│ upstream       │              │ osint-scout     │              │ evidence-qa      │
│ gas-lng        │◄── запросы ──│ licensed-data   │── evidence ─►│ case-writer      │
│ downstream-    │   фактов     └─────────────────┘              │ kb-librarian     │
│   chemicals    │                                                └──────────────────┘
│ services-      │
│   equipment    │
│ trading-       │
│   shipping     │
└────────────────┘
```

| Слой | Роль | Зона | Аккаунты |
|---|---|---|---|
| lead | `director` | вопрос → подвопросы, source plan, evidence ceiling, конфликты | — |
| segment | `upstream` | upstream-экономика, интегрированные E&P (владелец континуитета) | 10 |
| segment | `gas-lng` | газ, СПГ, спреды, фрахт, санкционная экспозиция | 4 |
| segment | `downstream-chemicals` | переработка, нефтехимия, TCO сырья, CAPEX | 4 |
| segment | `services-equipment` | сервис/оборудование как watch-list и база для TCO | 0 (нет в портфеле) |
| segment | `trading-shipping` | трейдинг, маршруты, фрахт, страхование | 2 |
| collection | `osint-scout` | открытые страницы и реестры, collection log | — |
| collection | `licensed-data` | подписки/лицензии, условия, запрет перепродажи | — |
| control | `evidence-qa` | grade/статус, независимая проверка, подпись `VERIFIED` | — |
| output | `case-writer` | кейс/one-pager из проверенного | — |
| control | `kb-librarian` | inbox → validate → merge → index | — |

Итого **11 узлов**: 1 lead + 10 ролей. Покрытие портфеля: 20 аккаунтов из
[`marketing/target_accounts.json`](../../marketing/target_accounts.json) распределены по
сегментным ролям ровно один раз (10 + 4 + 4 + 2).
Роль «энергетика и промышленные потребители» отложена (`deferred_roles`): в портфеле нет аккаунтов
сегмента, выдумывать их запрещено.

## 4. Процесс: вопрос → KB

```
QUESTION ─► SOURCE_PLAN ─► COLLECT ─► GRADE ─► ANALYSIS ─► CASE ─► QA ─► KB
director    director+role  osint/    evidence  segment     case-    evidence  kb-
                           licensed    -qa       role        writer    -qa     librarian
```

1. **QUESTION** — Director формулирует 1 вопрос и 3–5 проверяемых подвопросов, назначает владельца.
2. **SOURCE_PLAN** — класс источника (`playbooks/source_hierarchy.md`), ожидаемый grade,
   доступность; запрещённые источники исключаются до сбора.
3. **COLLECT** — `osint-scout` / `licensed-data` собирают с URL, датой, цитатой; нет сети →
   `NOT_FETCHED`.
4. **GRADE** — `evidence-qa` ставит A/B/C и статус, понижает завышенное.
5. **ANALYSIS** — сегментная роль строит сценарии/допущения только вокруг подтверждённого.
6. **CASE** — `case-writer` переносит проверенное в кейс, без новых чисел.
7. **QA** — вердикт `CONFIRMED / WITH_NOTES / NOT_CONFIRMED`, реестр дефектов.
8. **KB** — `kb-librarian` валидирует запись, сливает, пересобирает индекс
   ([`kb_workflow.md`](kb_workflow.md)).

Состояние задачи не переходит дальше, пока у артефакта нет статуса и ссылки на источник.

## 5. Артефакты

| Файл | Что внутри |
|---|---|
| [`org.json`](org.json) | схема сети: роли, сегменты, покрытие аккаунтов, процесс, границы, gates |
| [`roles/`](roles/) | 11 промптов ролей: мандат, входы, можно/нельзя, формат, критерий, передача |
| [`playbooks/lawful_osint.md`](playbooks/lawful_osint.md) | законный OSINT, закрытые базы, код, санкции |
| [`playbooks/source_hierarchy.md`](playbooks/source_hierarchy.md) | уровни источников, доступ, лицензия → grade |
| [`playbooks/evidence_grading.md`](playbooks/evidence_grading.md) | A/B/C, `VERIFIED/PARTIAL/HYPOTHESIS/NOT_FETCHED`, таблица решений |
| [`playbooks/red_lines.md`](playbooks/red_lines.md) | RL-01…RL-18, процедура остановки |
| [`kb_workflow.md`](kb_workflow.md) | как пополняется база знаний |
| [`../../tools/market_intel.py`](../../tools/market_intel.py) | валидатор сети и генератор task brief |
| [`../../tools/kb.py`](../../tools/kb.py) | add/search/validate/merge/index/export базы знаний |

## 6. Как запускать

```powershell
python tools/market_intel.py --check        # exit 0: схема, роли, промпты, покрытие, review
python tools/market_intel.py --org          # орг-схема и процесс человеку
python tools/market_intel.py --json         # машинная сводка (amfe.market_intel.check.v1)
python tools/market_intel.py --role upstream --question "Как шок цены меняет бюджет?" --account TATNEFT
python tools/market_intel.py --role osint-scout --question "..." --out .tmp/brief.md
python -m unittest tests.test_market_intel
```

Генератор brief **не ходит в сеть** и ничего не выдумывает: данные об аккаунте берутся из
`marketing/target_accounts.json` и помечаются `HYPOTHESIS`, остальное — `NOT_FETCHED` с причиной.

## 7. Границы применимости

- В среде нет доступа к сети и внешним БД: `--check` проверяет структуру, а не факты о рынке.
- Все записи KB — `HYPOTHESIS`/`NOT_FETCHED`/`PARTIAL` до появления первоисточника.
- Роль `evidence-qa` не заменяет человека-подпись; санкционные и правовые выводы — за человеком.
- Сеть не коммитит и не публикует: интеграция — Lead.
