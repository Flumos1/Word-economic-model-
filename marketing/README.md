# Marketing kit: нефть и газ

Комплект для выхода на нефтегазовый рынок с продуктом AMFE (сценарии, закупки, газ/СПГ,
геополитика, API).

## Файлы

| Файл | Что |
|---|---|
| [target_accounts.json](target_accounts.json) | 20 целевых компаний, 6 критериев, веса |
| [target_accounts.md](target_accounts.md) | рейтинг и таблица (генерируется `tools/marketing_targets.py`) |
| [one_pager.md](one_pager.md) | одностраничник для первого контакта |
| [outreach.md](outreach.md) | 3 последовательности писем + LinkedIn + скрипт звонка |
| [outreach_5.md](outreach_5.md) | первые 5 персонализированных касаний: персона, боли, use cases, письма, вложения, критерий успеха |
| [research/](research/README.md) | 10 досье аккаунтов: боли, buying center, триггеры, use cases, discovery-вопросы (валидируется `tools/account_research.py`) |
| [cases/](cases/manifest.json) | три двухстраничных кейса (бюджет/CAPEX, переработка, TCO/закупки) + PDF и SHA-256-манифест (собирается `tools/build_cases.py`, описание — [docs/208](../docs/208_Коммерческие_кейсы.md)) |
| [letters/](letters/) | готовые тексты писем по 5 аккаунтам (только плейсхолдеры, не отправлены) |
| [crm_pipeline.csv.tmpl](crm_pipeline.csv.tmpl) | 20 аккаунтов, стадии, next_action/next_date (валидируется `tools/marketing_crm.py`) |
| [discovery_questions.md](discovery_questions.md) | 20 вопросов для квалификации |
| [pricing.md](pricing.md) | офферы и ориентиры цен |
| [plan_90_days.md](plan_90_days.md) | календарь, каналы, KPI, бюджет |
| [../docs/191_Маркетинг_нефтегаз.md](../docs/191_Маркетинг_нефтегаз.md) | исследование рынка и стратегия |

## Пересобрать рейтинг

```powershell
python tools/marketing_targets.py --generated-at 2026-11-19T00:00:00Z
```

Скрипт проверяет схему/диапазоны, считает балл `0.35*fit + 0.20*digital + 0.15*procurement +
0.15*access + 0.10*budget + 0.05*risk`, присваивает Tier A/B/C и пишет Markdown.

## Проверить CRM и пакет писем

```powershell
python tools/marketing_crm.py --check     # exit 0/2: схема, стадии, tier, даты, нет контактов
python tools/marketing_crm.py --summary   # сводка по стадиям и следующие действия
python tools/marketing_crm.py --json      # машиночитаемая сводка (amfe.marketing.crm.v1)
python tools/account_research.py --check  # 10 досье: схема, evidence, нет контактов
python -m unittest tests.test_marketing_crm
python -m unittest tests.test_account_research
```

Валидатор fail-closed: дубли id/компаний, неизвестная стадия, битая дата, tier не из пересчёта,
недостача аккаунтов, адрес или телефон в любом поле → exit 2. Файлы не пишет.

## Собрать и проверить кейсы

```powershell
python tools/build_cases.py            # Markdown -> DOCX -> PDF + marketing/cases/manifest.json
python tools/build_cases.py --check    # сверка SHA-256, размеров и числа страниц, ничего не пишет
python -m unittest tests.test_cases    # 17 тестов: файлы, PDF, числа против источников, no-client, no-raw
```

Правило: сначала правится Markdown, потом пересобирается PDF; руками хеши в манифесте не правятся.

## Правила использования

- Оценки — **экспертные допущения**, не рейтинг компаний; проверять на discovery.
- Не отправлять внешним адресатам без проверки фактов о компании и NDA-рамки.
- В материалах всегда: `RESEARCH_ONLY`, `admission=false`, «не прогноз».
- Данные клиента — только по NDA, обезличенные, не в git.

> CRM хранится как `crm_pipeline.csv.tmpl` (синтетическая схема, не реальные данные). Скопируйте в `.csv` для работы.
