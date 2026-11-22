# Факт-пак: BP и Cheniere, FY2025

- Дата сборки: **2026-11-22** · `source_date` для всех 40 фактов: **2026-11-22**
- Статус: `FACT_PACK` / `RESEARCH_ONLY` · `admission=false` · `not_client=true` · не оферта, не прогноз, не для отправки без проверки человеком
- Метод: только официальные документы компаний — BP Annual Report and Form 20-F 2025 (PDF, 396 с.) и Cheniere Form 10-K FY2025 (SEC). PDF BP извлечён `pypdf 6.14.2`, 10-K — `BeautifulSoup/lxml`; каждая цитата проверена как подстрока извлечённого текста (40/40), длина ≤300 символов (максимум — 225)
- `loc`/ссылка для BP — **номер страницы PDF**, не печатный номер; для Cheniere — раздел 10-K (нумерация HTML-документа нестабильна)
- Шкала evidence: **A** — прямая цитата из первоисточника компании; все 40 строк — A. Гипотез и guidance в пакете нет: плановые мощности помечены в поле `period`
- Числа не подтверждают боль кейсов `wc-007`/`wc-008` (BP) и `wc-017`/`wc-018` (Cheniere): боль остаётся `HYPOTHESIS`

Поля строки — по заданию: `label · value · unit · period · source_id · source_date · evidence · quote (≤300) · url`.
Значения — в формате источника (десятичная запятая/точка как в отчёте: BP — `2,312`, Cheniere — `2,424`).

## 1. Источники

| source_id | документ | дата документа | формат | SHA-256 | URL |
|---|---|---|---|---|---|
| `SRC-BP-AR-2025` | BP Annual Report and Form 20-F 2025 (FY2025) | PDF создан 2026-03-05, изменён 2026-03-06 (метаданные); 20-F подан в SEC 2026-03-06 | PDF, 396 с., 4 367 578 байт | `bbe4e34edda665ed3d5d84291b79d13118e31ca1355e794a43f11fd50ab8a237` | https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf |
| `SRC-BP-AR-2025` (SEC) | тот же отчёт как Form 20-F, accession 0000313807-26-000006, primary doc `bp-20251231.htm` | подан 2026-03-06, период 2025-12-31 | HTML (Inline XBRL), 29 303 589 байт | `87afed212a3a189c223bd0c9fefdfd80d4f98f5076eea4a59f8bc8564bf0bd4d` | https://www.sec.gov/Archives/edgar/data/313807/000031380726000006/bp-20251231.htm |
| `SRC-LNG-AR-2025` | Cheniere Energy, Inc. Form 10-K за 2025, accession 0000003570-26-000005, primary doc `lng-20251231.htm` | подан 2026-02-26, период 2025-12-31 | HTML (Inline XBRL), 3 155 065 байт | `fd24b5862f515b2d461f0376a924dde62b7f6cb9941ea9ddb6cbd988095672b9` | https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm |

Проверка доступности на 2026-11-22: PDF bp.com — HTTP 200, `application/pdf`, 4 367 578 байт (перепроверено дважды);
оба документа SEC — HTTP 200 из `www.sec.gov/Archives`. Запросы шли с UA, объявляющим исследовательский
контакт; SEC ограничивает автоматический доступ (см. §4.3).

## 2. BP — 20 фактов FY2025 (добыча, запасы, сегменты, СПГ, страны, проекты)

| ID | label | value | unit | period | source_id | source_date | evidence | quote (≤300) | url |
|---|---|---|---|---|---|---|---|---|---|
| BP-01 | Добыча углеводородов, группа (net of royalties) | 2,312 | mboe/d | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | Total hydrocarbons (mboe/d) 2,312 2,358 2,313 | [PDF с. 29](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=29) |
| BP-02 | Добыча жидких углеводородов, группа | 1,199 | mb/d | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | Liquids (mb/d) 1,199 1,166 1,115 | [PDF с. 29](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=29) |
| BP-03 | Добыча природного газа, группа | 6,450 | mmcf/d | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | Natural gas (mmcf/d) 6,450 6,914 6,944 | [PDF с. 29](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=29) |
| BP-04 | Доказанные запасы углеводородов, группа (net of royalties) | 6,191 | mmboe (= 6.2 bn boe) | на 31.12.2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | 6,191mmboe proved hydrocarbon reserves for the groupb | [PDF с. 14](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=14) |
| BP-05 | Доказанные запасы жидких углеводородов, группа | 3,447 | mmb | на 31.12.2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | Liquids (mmb) 3,447 3,699 3,747 | [PDF с. 29](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=29) |
| BP-06 | Доказанные запасы природного газа, группа | 15,916 | bcf | на 31.12.2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | Natural gas (bcf) 15,916 14,786 17,471 | [PDF с. 29](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=29) |
| BP-07 | Сегмент oil production & operations: добыча | 1,527 | mboe/d | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | Total hydrocarbons (mboe/d) 1,527 1,470 1,383 | [PDF с. 35](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=35) |
| BP-08 | Сегмент oil production & operations: доказанные запасы | 4,993 | mmboe | на 31.12.2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | Total hydrocarbons« (mmboe) 4,993 4,932 5,142 | [PDF с. 35](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=35) |
| BP-09 | Сегмент gas & low carbon energy: добыча | 785 | mboe/d | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | Total hydrocarbons (mboe/d) 785 888 929 | [PDF с. 32](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=32) |
| BP-10 | Сегмент gas & low carbon energy: доказанные запасы | 1,198 | mmboe | на 31.12.2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | Total hydrocarbons«c (mmboe) 1,198 1,316 1,618 | [PDF с. 32](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=32) |
| BP-11 | Стран присутствия (корпоративный KPI) | 61 | countries | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | 61 countries of operation | [PDF с. 14](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=14) |
| BP-12 | Производство СПГ на upstream-активах (equity) | 12 | Mt | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | In 2025 our production was 12 million tonnes (Mt) of LNG from these assets, of which 5.2Mt were optimized and delivered through supply, trading and shipping (ST&S) | [PDF с. 342](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=342) |
| BP-13 | Долгосрочный стратегический портфель СПГ | 26.8 | Mtpa | на конец 2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | a global long-term strategic LNG portfolio of 26.8Mtpa | [PDF с. 342](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=342) |
| BP-14 | Инкрементальные merchant-объёмы СПГ (short/mid-term) | 14.7 | Mtpa | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | bp delivered 14.7Mtpa in 2025 of incremental merchant volumes through short and mid-term cargoes managed through the ST&S LNG business | [PDF с. 342](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=342) |
| BP-15 | Tangguh LNG (Индонезия, bp 40.22%): мощность завода | 11.4 | Mtpa | 2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | The plant consists of three trains with total production capacity of 11.4Mtpa. | [PDF с. 345](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=345) |
| BP-16 | Atlantic LNG (Тринидад, bp ~43%): мощность, gross | ~12 | Mtpa | 2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | The total gross capacity of the LNG facility is approximately 12Mtpa, with three trains in operation. | [PDF с. 343](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=343) |
| BP-17 | North West Shelf LNG (Австралия): доля мощности, net | 2.26 | Mt/год | 2025 | `SRC-BP-AR-2025` | 2026-11-22 | A | bp’s net share of the capacity of NWS LNG trains is 2.26Mt (15.78% of 14.3Mtpa gross) of LNG per year. | [PDF с. 345](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=345) |
| BP-18 | Ruwais LNG (ОАЭ, bp 10%): планируемая мощность | 9.6 | Mtpa | план (FID ADNOC) | `SRC-BP-AR-2025` | 2026-11-22 | A | Ruwais LNG is planned to have two liquefaction trains with a total annual capacity of 9.6Mt per annum. | [PDF с. 344](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=344) |
| BP-19 | Крупных проектов-стартапов в 2025 | 7 | projects | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | delivered seven major project start-ups, five of which were ahead of schedule | [PDF с. 11](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=11) |
| BP-20 | GTA Phase 1 (Мавритания/Сенегал): грузов отгружено bp ST&S | 19 | cargoes | 2025 (FY) | `SRC-BP-AR-2025` | 2026-11-22 | A | 19 cargoes were lifted by bp’s ST&S organization | [PDF с. 31](https://www.bp.com/api/files/6cqieuqhq4no/master/33M2iHp8A6d07McKzKqLNP/12b5d4eccb4e02093d1ad9efc0d6a746/bp-annual-report-and-form-20f-2025.pdf#page=31) |

Примечания к таблице BP:
- BP-04: `6 191 mmboe` = 6,2 млрд б.н.э. (BBOE); в отчёте та же цифра вынесена в KPI на с. 14.
- BP-07/BP-08 — сегмент oil production & operations; BP-09/BP-10 — gas & low carbon energy. Третий сегмент (customers & products) добычу и запасы не раскрывает.
- BP-12: 12 Mt — производство СПГ на upstream-активах (equity), из них 5,2 Mt оптимизировано и продано через ST&S. Это не «продажи СПГ» и не сопоставимо напрямую с продажами Shell (72,9 Mt) из `wc-005`.

## 3. Cheniere — 20 фактов на 31.12.2025 (мощности, поезда, объёмы, контрагенты)

| ID | label | value | unit | period | source_id | source_date | evidence | quote (≤300) | url |
|---|---|---|---|---|---|---|---|---|---|
| LNG-01 | Общая производственная мощность (вкл. деботтлнекинг) | >60 | mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | Our total production capacity is expected to be over 60 mtpa of LNG, inclusive of estimated debottlenecking opportunities, of which over 9 mtpa was under construction and the remainder was in operation as of December 31, 2025 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-02 | Мощность в строительстве (все площадки) | >9 | mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | of which over 9 mtpa was under construction and the remainder was in operation as of December 31, 2025 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-03 | Sabine Pass (SPL): мощность в эксплуатации | >30 | mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | over 30 mtpa of total production capacity in operation from natural gas liquefaction facilities located in Cameron Parish, Louisiana at Sabine Pass | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-04 | Sabine Pass: одобрено FERC/DOE (FTA и non-FTA, по 1,661.94 Bcf/год) | 33 | mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | FTA countries (1) 1,661.94 33 1,661.94 33 Non-FTA countries 1,661.94 33 1,661.94 33 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-05 | Sabine Pass: резервуары / морские причалы | 5 / 3 | tanks / berths | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | is one of the largest LNG production facilities in the world with over 30 mtpa of total production capacity, five storage tanks and three marine berths | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-06 | Corpus Christi (CCL): ожидаемая мощность (вкл. деботтлнекинг) | >30 | mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | has over 30 mtpa of total expected production capacity, inclusive of estimated debottlenecking opportunities, including over 4 mtpa under construction from the Corpus Christi Stage 3 Project | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-07 | Corpus Christi Trains 1–3: одобрено FERC/DOE (875.16 Bcf/год) | 17 | mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | Trains 1 through 3 of the CCL Project: FTA countries 875.16 17 875.16 17 Non-FTA countries 875.16 17 875.16 17 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-08 | Corpus Christi Stage 3: поездов / мощность | 7 / >10 | trains / mtpa | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | a project consisting of seven midscale Trains that is expected to add total production capacity of over 10 mtpa of LNG once fully completed | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-09 | CCL Midscale Trains 8 & 9: поездов / мощность | 2 / ~5 | trains / mtpa | FID 17.06.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | a project consisting of two additional midscale Trains that is expected to add total production capacity of approximately 5 mtpa of LNG once fully completed | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-10 | SPL Expansion: поездов / мощность | 3 / до ~20 | trains / mtpa | заявка FERC обновлена 06.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | inclusive of three liquefaction trains and supporting infrastructure, maintaining an expected total peak production capacity of up to approximately 20 mtpa of LNG | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-11 | CCL Expansion: поездов / мощность | 4 / до 24 | trains / mtpa | заявка FERC 02.2026 | `SRC-LNG-AR-2025` | 2026-11-22 | A | inclusive of four liquefaction trains and supporting infrastructure, with an expected total peak production capacity of up to 24 mtpa of LNG | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-12 | Отгружено с Liquefaction Projects (2,400 operational + 24 commissioning) | 2,424 | TBtu | 2025 (FY) | `SRC-LNG-AR-2025` | 2026-11-22 | A | Volumes loaded during the current period 2,400 24 2,424 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-13 | Признано в выручке (total volumes recognized) | 2,439 | TBtu | 2025 (FY) | `SRC-LNG-AR-2025` | 2026-11-22 | A | Total volumes recognized in the current period 2,416 23 2,439 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-14 | Продано: volumes delivered as LNG revenues | 2,438 | TBtu | 2025 (FY) | `SRC-LNG-AR-2025` | 2026-11-22 | A | Total volumes delivered as LNG revenues 2,438 2,349 89 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-15 | Долгосрочных SPA-контрагентов (срок 10+ лет) | ~30 | customers | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | As of December 31, 2025, we had SPAs with initial terms of 10 or more years with approximately 30 different third party customers | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-16 | Кумулятивно отгружено грузов / тонн | >4,610 / >315 | cargoes / Mt | 02.2016 – 20.02.2026 | `SRC-LNG-AR-2025` | 2026-11-22 | A | over 4,610 cumulative LNG cargoes totaling over 315 million tonnes of LNG have been produced, loaded and exported from the Liquefaction Projects | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-17 | География поставок | >40 | countries | на 20.02.2026 | `SRC-LNG-AR-2025` | 2026-11-22 | A | Our LNG has been shipped to over 40 countries and regions around the world. | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-18 | Персонал (1,617 США / 100 вне США) | 1,717 | employees | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | As of December 31, 2025, we had 1,717 full-time employees with 1,617 located in the U.S. and 100 located outside of the U.S. | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-19 | LNG revenues (финансовый ориентир, не operational) | 19,435 | MUSD | 2025 (FY) | `SRC-LNG-AR-2025` | 2026-11-22 | A | Total LNG revenues $ 19,435 $ 14,899 $ 4,536 | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |
| LNG-20 | Позиция: крупнейший производитель СПГ в США, второй в мире по мощности | 1-е в США / 2-е в мире | rank | на 31.12.2025 | `SRC-LNG-AR-2025` | 2026-11-22 | A | As of December 31, 2025, we were the largest producer of LNG in the U.S. and the second largest LNG operator globally, based on the total production capacity of our natural gas liquefaction facilities. | [10-K](https://www.sec.gov/Archives/edgar/data/3570/000000357026000005/lng-20251231.htm) |

Примечания к таблице Cheniere:
- LNG-01/LNG-06: «>60 mtpa» и «>30 mtpa» — оценки компании, включают деботтлнекинг и мощности в строительстве; не гарантированная производительность.
- LNG-12/13/14 — три разных определения одного года: отгружено (loaded), признано в выручке (recognized), продано (delivered as LNG revenues). Не взаимозаменяемы.
- LNG-19 — единственная финансовая строка, включена как ориентир (не operational).

## 4. Ограничения

### 4.1. Как читать числа BP

1. Добыча и запасы — **net of royalties** (доля bp после роялти), а не working interest. В групповые цифры входят equity-accounted entities: 1 330 mmboe запасов и 380 mboe/d добычи из 2 312 mboe/d (с. 29).
2. «61 страна» — корпоративный KPI «Our organization» (с. 14), не разбивка по сегментам. Сегментов три: oil production & operations, gas & low carbon energy, customers & products; в пакете — два upstream-сегмента.
3. СПГ: 12 Mt — только upstream-производство (Абу-Даби, Ангола, Австралия, Индонезия, Мавритания/Сенегал, Тринидад и Тобаго); 26,8 Mtpa — долгосрочный портфель (equity + merchant); 14,7 Mtpa — инкрементальные short/mid-term объёмы 2025 года. Три разные вещи, складывать нельзя.
4. «7 стартапов» — корпоративный счётчик; в отчёте названы GTA, Cypre, Murlach, полного перечня одной строкой нет. Цифра 7 подтверждена, состав — частично.
5. Ruwais LNG — проект ADNOC (bp 10%), 9,6 Mtpa заявлены как план, а не действующая мощность. Atlantic LNG — gross 12 Mtpa при доле bp ~43%; NWS — net 2,26 Mt (15,78% от 14,3 Mtpa gross).

### 4.2. Как читать числа Cheniere

1. Мощности даны как **expected / up to / inclusive of debottlenecking** — оценка компании, а не гарантия. На 31.12.2025 более 9 mtpa ещё в строительстве.
2. Три объёма 2025: 2 424 TBtu отгружено (2 400 operational + 24 commissioning), 2 439 TBtu признано в выручке, 2 438 TBtu продано. Разница между ними — транзит и признание, а не ошибка.
3. Число поездов Sabine Pass в тексте 10-K прямо не указано: есть «over 30 mtpa», 5 резервуаров, 3 причала. Поэтому строки «поезда SPL» нет — честный пробел. По Corpus Christi поезда указаны: Trains 1–3, Stage 3 — 7 midscale, Trains 8 & 9 — 2, Expansion — 4.
4. «~30 контрагентов» — SPAs со сроком 10+ лет; компании под общим контролем считаются одним клиентом. Клиентов с долей 10%+ выручки нет (Item 1, Major Customers).
5. LNG revenues 19 435 MUSD — финансовый показатель (строка LNG-19), не operational; с объёмами в TBtu не взаимозаменяем.

### 4.3. Что не открылось (проверено 2026-11-22)

| Ресурс | Результат | Что сделано |
|---|---|---|
| `bp.com/en/global/corporate/investors/annual-report.html` (web_fetch) | HTTP 403, страница ошибки «Something went wrong» | PDF взят по ссылке `/api/files/...` с той же страницы; с браузерным UA — HTTP 200 |
| `bp.com/.../bp-annual-report-and-form-20f-2025.pdf` (прямой путь) | HTTP 200, но `text/html` — заглушка, не PDF | перешёл на ссылку `/api/files/...` |
| PDF BP при открытии | AES-зашифрован (пустой пароль), `pypdf` без AES не читает | доустановлен `cryptography` 50.0.2; извлечено 396 с., 1 751 890 символов |
| `ir.cheniere.com` | ConnectionError (соединение/DNS) | 10-K взят из SEC Archives |
| `cheniere.com/investors/financial-information`, `/investors/annual-report` | HTTP 404 | PDF-версии 10-K нет; официальный документ — HTML в SEC |
| SEC `cgi-bin/browse-edgar` | блокировка «Undeclared Automated Tool» | использованы `data.sec.gov/submissions` и `www.sec.gov/Archives` |
| SEC `company_tickers.json` | «Request Rate Threshold Exceeded» | CIK найден повторным запросом после паузы |
| `web_search` | нет API-ключа (`DEEPSEEK_API_KEY`) | поиск не использовался; только прямые официальные URL |

### 4.4. Границы пакета

- Контактов, имён, телефонов, e-mail в пакете нет и быть не должно.
- Финансы BP (прибыль, CAPEX, дивиденды) не входят: задание — добыча, запасы, СПГ, страны, проекты. У Cheniere финансы ограничены одной строкой LNG revenues.
- Числа — исторические (FY2025) либо плановые; плановое помечено в `period`.
- `RESEARCH_ONLY`, `admission=false`: не допуск модели, не КП, не инвестиционная рекомендация. Использование в письмах — только после проверки человеком.

## 5. Метод проверки

1. Источники найдены через SEC `data.sec.gov/submissions` по CIK (BP 313807, Cheniere 3570); отчёт BP дополнительно скачан как PDF с bp.com.
2. PDF BP извлечён `pypdf` 6.14.2 постранично (маркеры `=== PAGE n ===`), HTML 10-K — `BeautifulSoup`/`lxml`. Нормализация: перенос строки → пробел, дефисный перенос склеен, повторные пробелы сжаты.
3. Каждая цитата проверена как **подстрока** нормализованного текста источника — 40/40 найдены; длина ≤300 (максимум 225).
4. Номера страниц BP (`url#page=`) получены поиском цитаты по страницам PDF, а не на глаз.
5. SHA-256 скачанных файлов зафиксированы в §1 — пакет можно перепроверить побайтово.
6. Цитаты LNG-04, LNG-07, LNG-12—LNG-14 — строки таблиц 10-K: ячейки в порядке отчёта (2025, 2024), числа не переставлены.
7. `«` в цитатах BP-08/BP-10 — артефакт извлечения надстрочной сноски (`a`/`c`) в PDF; на этом месте в отчёте сноска к таблице, число не искажено.

## 6. Что не менялось

`marketing/western_cases/cases.json`, `tests/`, `tools/` не тронуты; каталог (`catalog.md`, `catalog_en.md`, PDF) не перегенерировался. Этот файл — отдельный факт-пак, а не часть `cases.json`. Перенос чисел в кейсы — отдельный шаг с прогоном `tests/test_western_cases.py`.

## 7. Быстрая выжимка

- BP FY2025: добыча 2 312 mboe/d (−2% к 2024), запасы 6 191 mmboe (−1%), 61 страна, upstream-СПГ 12 Mt, портфель 26,8 Mtpa, 7 крупных стартапов.
- Cheniere на 31.12.2025: >60 mtpa суммарно (>9 mtpa в стройке), Sabine Pass >30 mtpa, Corpus Christi >30 mtpa, отгружено 2 424 TBtu за 2025, ~30 долгосрочных контрагентов.
