# 221. Факт-паки Chevron, TotalEnergies, BP, Cheniere

Дата: 2026-11-22. Статус: **RESEARCH_ONLY, admission=false, not_client=true**. Числа из открытых
годовых отчётов; не клиентские данные и не прогноз.

## 1. Источники

| source_id | Документ | URL | Дата |
|---|---|---|---|
| `SRC-CVX-AR-2025` | Chevron 2025 Annual Report (123 стр.) | https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf | 2026-11-22 |
| `SRC-TTE-AR-2025` | TotalEnergies Universal Registration Document 2025 (666 стр.) | https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf | 2026-11-22 |
| `SRC-BP-AR-2025` | BP Annual Report and Form 20-F 2025 (396 стр.) | https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf | 2026-11-22 |
| `SRC-LNG-AR-2025` | Cheniere Form 10-K FY2025 | https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm | 2026-11-22 |

Полные цитаты, страницы и ограничения — в рабочих файлах:
[`facts_chevron_total.md`](../marketing/western_cases/facts_chevron_total.md),
[`facts_bp_cheniere.md`](../marketing/western_cases/facts_bp_cheniere.md).

## 2. Числа, добавленные в кейсы

### Chevron (`wc-003`, `wc-004`)

| Факт | Значение |
|---|---|
| Добыча 2025 | **3 723 MBOED** |
| Доказанные запасы на 31.12.2025 | **10 591 MMBOE** |
| Отчётных сегментов | **2** |
| Юрисдикций существенной деятельности | **22** |
| Добыча газа | **8 515 MMCFD** |
| Добыча Permian | **1 MMBOED** |

### BP (`wc-007`, `wc-008`)

| Факт | Значение |
|---|---|
| Добыча 2025 | **2 312 mboe/d** |
| Жидкие углеводороды | **1 199 mb/d** |
| Газ | **6 450 mmcf/d** |
| Доказанные запасы | **6 191 mmboe** |
| Стран присутствия | **61** |
| Долгосрочный портфель СПГ | **26,8 Mtpa** |

### TotalEnergies (`wc-009`, `wc-010`)

| Факт | Значение |
|---|---|
| Добыча 2025 | **2 529 kboe/d** |
| Доказанные запасы | **11 218 MBOE** |
| Продажи СПГ 2025 | **43,9 Mt** |
| Портфель СПГ | **44 Mt** |
| Бизнес-сегментов | **5** |
| Стран присутствия | **~120** |

### Cheniere (`wc-017`, `wc-018`)

| Факт | Значение |
|---|---|
| Суммарная мощность | **>60 mtpa** |
| Sabine Pass | **>30 mtpa** |
| Corpus Christi | **>30 mtpa** |
| Отгружено в 2025 | **2 424 TBtu** |
| Стран получателей | **>40** |
| Долгосрочных SPA-контрагентов | **~30** |

## 3. Ограничения

1. Все числа — за 2025 или «expected/up to»; guidance и факт не смешивать.
2. Chevron: `10 591 MMBOE` — net proved reserves; `3 723 MBOED` — net oil-equivalent.
3. BP: добыча net of royalties и включает equity-accounted; `12 Mt` upstream LNG ≠ продажи
   СПГ; `26,8 Mtpa` — долгосрочный портфель, не сумма 12 + 14,7.
4. TotalEnergies: `11 218 MBOE` — combined proved (1P); `43,9 Mt` — продажи СПГ, не сжижение.
5. Cheniere: мощности включают debottlenecking и «expected»; `2 424 TBtu` — отгружено, не продано.
6. Контактов, ФИО, телефонов нет; компании — не клиенты.

## 4. Что обновлено

- `cases.json`: `facts[]` у 8 кейсов (Chevron, BP, TotalEnergies, Cheniere).
- `catalog.md`/`catalog.pdf` + EN: приложение «Факт-паки» содержит все числа.
- `docs/214`: каталог 102 кейса / 51 компания; факт-паки перечислены.
- Сайт: страница 102 кейса.

## 5. Следующее

1. Проверить числа человеком по страницам PDF (ссылки `#page=N` в рабочих файлах).
2. Добавить факт-паки ExxonMobil, Eni, Equinor (если ещё нет) и обновить `facts[]`.
3. Не отправлять письма до проверки адресатов.
