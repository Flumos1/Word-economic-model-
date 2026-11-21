# 220. Shell и Equinor: числа из открытых отчётов

Дата: 2026-11-21. Статус: **RESEARCH_ONLY, admission=false, not_client=true**. Факт-пак по
открытым годовым отчётам; не клиентские данные, не прогноз.

## 1. Источники

| ID | Источник | URL | Дата |
|---|---|---|---|
| SRC-SHELL-AR-2025 | Shell Annual Report and Accounts 2025 (PDF, 474 стр.) | https://www.shell.com/investors/results-and-reporting/annual-report/_jcr_content/root/main/section/promo.multi.stream/1779352356739/36306d968747b6079d3f800b2b1552a033856b5d/shell-integrated-annual-and-sustainability-report.pdf | 2026-11-21 |
| SRC-EQNR-AR-2025 | Equinor 2025 Annual Report (PDF, 324 стр.) | https://cdn.sanity.io/files/h61q9gi9/global/9b3eac2a01f1ce70efa5c751106472c1a59a0ee3.pdf | 2026-11-21 |

## 2. Shell: факты

| # | Факт | Значение | Evidence | Примечание |
|---|---|---|---|---|
| S1 | Добыча, доступная к продаже, 2025 | **2 800 kboe/d** | A | 2024: 2 836 |
| S2 | Сжижение СПГ, 2025 | **28,4 Mt** | A | 2024: 29,1 |
| S3 | Продажи СПГ, 2025 | **72,9 Mt** | A | 2024: 65,8; рост за счёт Pavilion Energy |
| S4 | Доказанные запасы на 31.12.2025 | **8 123 MBOE** | A | снижение на 1 497 million BOE за год |
| S5 | Стран присутствия | **>70** | A | «more than 70 countries» |
| S6 | Сотрудников | **~85 000** | A | в кейсах не используем как финансовый факт |

## 3. Equinor: факты

| # | Факт | Значение | Evidence | Примечание |
|---|---|---|---|---|
| E1 | Equity production, 2025 | **2 137 mboe/d** | A | рекорд; NCS + международный портфель |
| E2 | Доказанные запасы на конец 2025 | **5 183 million BOE** | A | proved reserves, entitlement volumes |
| E3 | Стран присутствия | **>20** | A | «more than 20 countries» |
| E4 | Сотрудников | **~24 600** | A | в кейсах не используем |
| E5 | Hammerfest LNG | **6,5 BCM/год**, доля **37 %** | A | LNG plant, Norway |
| E6 | Добыча EPI, 2025 | **1 410 mboe/d** | A | E&P International |
| E7 | Добыча E&P USA, 2025 | **434 mboe/d** | A | подразделение E&P USA |

## 4. Что обновлено

- **`wc-005`** (Shell, трейдинг/Ормуз): добавлены S1–S5 — добыча, сжижение, продажи СПГ, запасы,
  страны. Вопрос о стоимости закрытия пролива теперь опирается на реальный масштаб СПГ-портфеля.
- **`wc-006`** (Shell, бюджет/геополитика): те же факты как база масштаба; гипотеза боли остаётся
  гипотезой.
- **Equinor:** отдельных кейсов в каталоге 100 нет; факты E1–E7 — в этом документе и в досье
  `marketing/western_research/equinor.md`; можно добавить 2 кейса отдельной волной.

## 5. Ограничения

1. Shell: `2 800 kboe/d` — **production available for sale**, не «добыча жидкости»; не путать с
   `1,4 million barrels per day` (liquids production target).
2. Shell: `72,9 Mt` — **продажи СПГ**, `28,4 Mt` — **сжижение**; это разные метрики.
3. Equinor: `2 137 mboe/d` — **equity production**, не вся добыча группы.
4. `5 183 million BOE` и `8 123 MBOE` — proved reserves на конец 2025; не «ресурсы».
5. Числа — за 2025; 2026 guidance не смешиваем.
6. Контактов, ФИО, телефонов нет; компании — не клиенты.

## 6. Следующее

- Добавить Equinor 2 кейса в каталог (51-я компания) отдельной волной — аккуратно, с тестами на 102.
- Проверить числа человеком по страницам PDF.
- Обновить `catalog.pdf` приложением с факт-паками.
