# 154. G2: интеграция ABM → счета SFC

Версия 1.0.0 · 2026-11-16. Волна 22, ворота G2 (план — [docs/145](145_AMFE_2.0_план.md)). Задача:
`task-46`. Код: [src/amfe/agentic/accounts_bridge.py](../src/amfe/agentic/accounts_bridge.py),
демо: [tools/agentic_accounts_demo.py](../tools/agentic_accounts_demo.py), тесты:
[tests/test_agentic_accounts.py](../tests/test_agentic_accounts.py).

Мост проверяет, что агрегаты агентного симулятора ([docs/150](150_Агентный_симулятор.md)) можно
передать в учётное ядро/SFC ([docs/77](77_Учётное_ядро_этап2.md)) без потери
сумм: каждая транзакция двойной записи остаётся сбалансированной, финансовые позиции зеркальны,
а `S = I + CA` (`CA = 0`) выполняется на уровне агрегатов. Это **интеграционный тест контуров**, а не
калиброванная модель и не прогноз.

## 1. Архитектура

```
AgenticSimulator (L5)
  └─ Ledger: финансовые stock-счета (депозиты/кредиты), текущие потоки (доходы/расходы),
             реальный капитал и memo-счёт инвестиций
        │
        ▼  AccountsBridge (read-only adapter)
  стандартные агрегаты SFC: household / firm / bank / government
    income, expenditure, saving, investment,
    financial assets / liabilities, net financial worth, real assets, net worth
        │
        ▼  следующий шаг (G2+, вне этой задачи)
  учётное ядро accounts.AccountSystem / мировая модель: счета-инструменты, периоды, внешний сектор
```

Мост только читает; `simulator.py`, `accounts/**`, `world/**` и frozen-файлы не меняются. Мост не
пересчитывает поведение: он раскладывает ledger по категориям и **падает (fail-closed)**, если
суммы не сходятся.

## 2. Маппинг категорий

| Счёт симулятора | Сектор | Измерение / код (ядро) | Категория |
|---|---|---|---|
| `hh.i.deposit` | household | financial / `deposit` | финансовый актив |
| `firm.j.deposit` | firm | financial / `deposit` | финансовый актив |
| `firm.j.loan` | firm | financial / `loan` | обязательство (знак −) |
| `firm.j.capital` | firm | real / `fixed_capital` | реальный актив |
| `gov.deposit` | government | financial / `deposit` | финансовый актив |
| `gov.loan` | government | financial / `loan` | обязательство (знак −) |
| `bank.deposit_liability` | bank | financial / `deposit` | обязательство (знак −) |
| `bank.loan`, `bank.gov_loan` | bank | financial / `loan` | финансовые активы |
| `hh.*.wage_income`, `transfer_income`, `interest_income` | household | current / `wages`, `transfer`, `interest` | доход |
| `hh.*.consumption`, `tax`, `interest_paid` | household | current / `household_consumption`, `direct_tax`, `interest` | расход |
| `firm.j.sales` | firm | current / `output` | доход |
| `firm.j.wage_cost`, `procurement_cost`, `interest_paid`, `tax` | firm | current / `wages`, `intermediate_consumption`, `interest`, `direct_tax` | расход |
| `firm.j.investment` | firm | capital / `fixed_investment` | memo (не в текущем сбережении) |
| `gov.tax_income` | government | current / `direct_tax` | доход |
| `gov.transfer_paid`, `spending` | government | current / `transfer`, `government_consumption` | расход |
| `bank.interest_income`, `interest_paid` | bank | current / `interest` | доход / расход |
| `memo.investment_goods` | — | capital / `investment_goods` | memo (исключён из агрегатов) |

Секторы и коды совпадают со словарём `amfe.accounts.model` (`Sector`, `CurrentFlow`, `Dimension`,
`FinancialInstrument`, `RealAsset`), чтобы следующий шаг мог писать напрямую в типизированный chart.
**Пробел ядра:** промежуточное потребление (`procurement_cost`) не имеет кода в `CurrentFlow`;
мост экспортирует его как `current:intermediate_consumption` — это расширение, зафиксированное здесь.

## 3. Инварианты (fail-closed, `AccountsBridgeError`)

| Проверка | Что проверяет |
|---|---|
| `double_entry` | каждая запись ledger: сумма postings = 0 (повторно, независимо от симулятора) |
| `ledger_total` | ledger (финансовые + реальные + memo) в сумме = 0 |
| `financial_mirroring` | сумма подписанных финансовых позиций по секторам = 0 |
| `sector_saving` | `saving = income + expenditure` по каждому сектору (расходы отрицательны) |
| `sfc_identity` | `S = I + CA`, `CA = 0` (закрытая экономика, нет `ext.*` счетов) |
| `net_worth` | `NW = net financial worth + real assets` по секторам и в сумме |
| `closed_economy` | в ledger нет внешних счетов |
| `result_consistency` | digest/число транзакций ledger и суммы saving/investment за периоды совпадают с `SimulationResult` |

Неизвестный счёт, несовпадение ledger с результатом, испорченная транзакция или нарушение любой
проверки → исключение, агрегаты не выдаются (в демо — код выхода `2`).

## 4. API

```python
from amfe.agentic.simulator import AgenticSimulator, Parameters
from amfe.agentic.accounts_bridge import (
    AccountsBridge, build_account_aggregates, export_sector_accounts,
    validate_against_sfc, render_report, AccountsBridgeError,
)

sim = AgenticSimulator(Parameters(periods=16, seed=20261116))
result = sim.run()
aggregates = build_account_aggregates(result, sim.ledger)   # AccountAggregates
aggregates.sectors["firm"].saving                            # Decimal
aggregates.totals                                            # income/expenditure/saving/investment/CA
aggregates.checks                                            # dict[str, bool]
aggregates.as_dict(); aggregates.to_json(); render_report(aggregates)
export_sector_accounts(result, sim.ledger)                   # {sector: {...}} JSON-safe
validate_against_sfc(result, sim.ledger)                     # checks, fail-closed
AccountsBridge(result, sim.ledger).build(raise_on_fail=False)  # агрегаты без исключения
```

Демо: `python tools/agentic_accounts_demo.py` (текст) или `--json` (JSON; `--out PATH` — записать в
файл). Код выхода `0`, если все проверки прошли, иначе `2`.

## 5. Числа демо (16 периодов, seed 20261116; шок в периоде 8; ИИ-доля закупок 0 → 50% с периода 4)

Агрегаты за весь прогон (ден. ед.):

| Сектор | baseline: income / exp. / saving / inv. | шок: income / exp. / saving / inv. | шок + ИИ: income / exp. / saving / inv. |
|---|---|---|---|
| household | 320.000 / −320.000 / 0.000 / 0 | 294.000 / −294.000 / 0.000 / 0 | 294.000 / −294.000 / 0.000 / 0 |
| firm | 476.336 / −353.750 / 122.586 / 79.389 | 447.051 / −310.146 / 136.905 / 74.508 | 440.415 / −304.616 / 135.799 / 73.402 |
| bank | 4.804 / 0.000 / 4.804 / 0 | 4.804 / 0.000 / 4.804 / 0 | 4.804 / 0.000 / 4.804 / 0 |
| government | 64.000 / −112.000 / −48.000 / 0 | 58.800 / −126.000 / −67.200 / 0 | 58.800 / −126.000 / −67.200 / 0 |
| **итого** | income 865.140, **S = I = 79.389**, CA = 0 | income 804.655, **S = I = 74.508**, CA = 0 | income 798.019, **S = I = 73.402**, CA = 0 |

Финансовые позиции (конец прогона, baseline): фирмы — активы 47.354, обязательства −4.158, net 43.196;
банк — активы 52.158, обязательства −47.354, net 4.804; государство — net −48.000; домохозяйства —
net 0. Сумма net financial worth по всем секторам = **0** (зеркальность), суммарный net worth =
реальные активы = 79.389.

Все три сценария проходят все проверки §3: `double_entry`, `ledger_total`, `financial_mirroring`,
`sector_saving`, `sfc_identity`, `net_worth`, `closed_economy`, `result_consistency` = True.
Направления: шок снижает доходы и инвестиции; ИИ-доля в закупках дополнительно снижает издержки и
инвестиции (цены — в [docs/150](150_Агентный_симулятор.md) §6), но **это условная иллюстрация
согласованности, а не оценка реального эффекта**.

## 6. Ограничения

1. **Агрегаты кумулятивные, не периодические.** Ledger симулятора не помечает периоды; мост
   агрегирует за весь прогон. Для периодных счетов нужен period-tag в ledger или экспорт
   `PeriodResult`-потоков (следующий шаг, без изменения `simulator.py`).
2. **Не все счета ядра представлены:** нет bonds, капитальных трансфертов, начислений/переоценок,
   запасов (inventory), внешнего сектора и ЦБ; промежуточное потребление — расширение
   `intermediate_consumption` (в `CurrentFlow` ядра его нет).
3. **Домохозяйства «hand-to-mouth»:** потребление ограничено депозитом, поэтому в демо их saving =
   0; это свойство игрушечной модели, а не вывод о реальных домохозяйствах.
4. **Упрощённые финансовые позиции:** только депозиты и кредиты; капитал — memo-реальный актив;
   инвестиции = Δfixed_capital (амортизации нет), поэтому `I` — валовые инвестиции без выбытия.
5. **Закрытая экономика:** `CA = 0` по построению; интеграция с миром потребует внешнего сектора
   (`CA ≠ 0`) и двусторонних позиций.
6. **Не калибровано:** числа демо не являются прогнозом, оценкой политики или эффекта ИИ; никаких
   claims о реальной экономике.

## 7. Следующий шаг к мировой модели

1. **Period-tag в ledger** (следующая волна, изменение `simulator.py` — не в этой задаче): экспорт
   периодных `journal`-записей в формате `accounts.JournalEntry`/`Posting` (sector, dimension, code,
   amount) без пересчёта поведения.
2. **Типизированная запись в ядро:** `AccountSystem.apply(Transaction)` для каждого периода;
   проверка `invariants.check_report` (financial mirroring, `ΔNW = saving + revaluation`,
   government budget, GDP identity) на агрегатах ABM.
3. **Внешний сектор:** добавить экспорт/импорт и валюты, тогда `S = I + CA` с `CA ≠ 0`; зеркальность
   финансовых позиций — между странами.
4. **Мировая модель:** передача агрегатов ABM (S, I, CA, доходы/расходы секторов) в L3 как
   замыкающих условий; проверка, что сценарии мира не нарушают SFC.
5. **Калибровка:** моментный мэтчинг на SFC/MRIO-данных (доли труда/капитала, норма сбережения,
   кредитная нагрузка) — только после period-tag и внешнего сектора, с предрегистрацией критериев.

## 8. Тесты

`tests/test_agentic_accounts.py` — 19 тестов, все зелёные: полнота и словарь маппинга; сохранение
сумм (income/expenditure/финансовые позиции); повторная проверка двойной записи; financial
mirroring; `S = I + CA`; тождества net worth; сверка с `SimulationResult`; JSON/`export`-сериализация;
детерминизм агрегатов и отчёта; fail-closed (неизвестный счёт, чужой ledger, испорченная
транзакция, неверные типы); три сценария демо; CLI (`--json`, текст, коды выхода).

```powershell
python tests/test_agentic_accounts.py            # 19 тестов
python tools/agentic_accounts_demo.py            # текст
python tools/agentic_accounts_demo.py --json     # машиночитаемый вывод
```

## 9. Ссылки

- [150](150_Агентный_симулятор.md) — агентный симулятор (ABM, ledger, SFC-инварианты);
- [145](145_AMFE_2.0_план.md) — план AMFE 2.0 (трек E, ворота G2);
- [77](77_Учётное_ядро_этап2.md) — типизированное SFC-ядро (секторы, коды,
  инварианты);
- `src/amfe/agentic/accounts_bridge.py`, `tests/test_agentic_accounts.py`,
  `tools/agentic_accounts_demo.py` — первоисточники этого документа.
