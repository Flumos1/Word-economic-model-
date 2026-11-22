# Факт-паки: Chevron и TotalEnergies, FY2025

- Дата сборки: **2026-11-22** · `source_date` для всех фактов: **2026-11-22**
- Статус: `FACT_PACK` / `RESEARCH_ONLY` · `admission=false` · не оферта, не прогноз, не для отправки без проверки человеком
- Метод: только официальные документы компаний (PDF), скачаны `Invoke-WebRequest` с браузерным User-Agent, текст извлечён `pypdf 6.14.2`, цитаты проверены полнотекстовым поиском; ключевые цифры Chevron сверены с SEC 10-K
- Страницы в таблицах — **номера страниц PDF**, не печатные номера (у Chevron расхождение 1–2 страницы)
- Шкала evidence: **A** — прямая цитата из первоисточника; **A−** — вывод из прямых данных (счёт, округление, отсутствие в полном тексте); **G** — guidance (прогноз компании, не факт)

## 1. Источники

| source_id | Документ | URL | Дата документа | Дата обращения | SHA-256 | Объём |
|---|---|---|---|---|---|---|
| `SRC-CVX-AR-2025` | Chevron 2025 Annual Report (включает Form 10-K за 2025) | https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf | 10-K подписан 2026-02-24; PDF создан 2026-04-06 | 2026-11-22 | `684FED77EC2056F644A528DB497D70E370EE8FD70EC5936CDD3325B09D26B309` | 123 стр. |
| `SRC-CVX-10K-2025` (сверка) | Chevron Form 10-K FY2025, SEC EDGAR | https://www.sec.gov/Archives/edgar/data/93410/000009341026000078/cvx-20251231.htm | filed 2026-02-24 | 2026-11-22 | — (HTML, сверка цитат) | — |
| `SRC-TTE-AR-2025` | TotalEnergies Universal Registration Document 2025 (including the annual financial report) | https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf | подан в AMF 2026-03-27; PDF пересобран 2026-09-22 | 2026-11-22 | `6A15F2D83D5CB910CC7111F23715FEC0F68AD00EE892A72042D59A8418C58CA7` | 666 стр. |

Локальные копии: `data/sources/2025_annual_reports/chevron-2025-annual-report.pdf`, `data/sources/2025_annual_reports/totalenergies-urd-2025.pdf`.

## 2. Chevron — факты FY2025

| ID | Показатель | Значение | Ед. | Период | Источник | Ev | Цитата | Стр./URL |
|---|---|---|---|---|---|---|---|---|
| C01 | Добыча, net oil-equivalent | 3 723 | MBOED (тыс. б.н.э./сут) | FY2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Net oil-equivalent production (Thousands of oil-equivalent barrels per day) 3,723 3,338 3,120» | [p. 22](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=22) |
| C02 | Добыча, округлённо | 3,7 | MMBOED (млн б.н.э./сут) | FY2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «The company's worldwide net oil-equivalent production in 2025 was 3.7 million barrels per day, 12 percent higher than in 2024 primarily due to the acquisition of Hess and growth in TCO, the Permian Basin and the Gulf of America» | [p. 32](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=32) |
| C03 | Доказанные запасы, net | 10 591 | MMBOE | 31.12.2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Net proved oil-equivalent reserves2 (Millions of barrels) 10,591 9,804 11,069» | [p. 22](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=22) |
| C04 | Доказанные запасы, округлённо | 10,6 | BBOE (млрд б.н.э.) | 31.12.2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Net proved reserves for consolidated companies and affiliated companies totaled 10.6 billion barrels of oil-equivalent at year-end 2025, an increase from year-end 2024.» | [p. 33](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=33) |
| C05 | Коэффициент замещения запасов | 158 | % | FY2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «The reserve replacement ratio in 2025 was 158 percent.» | [p. 33](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=33) |
| C06 | Отчётные сегменты | 2 | шт. (Upstream, Downstream) | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «The investments are grouped into two business segments, Upstream and Downstream, representing the company's "reportable segments" and "operating segments."» | [p. 74](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=74) |
| C07 | Страны/юрисдикции «существенной деятельности» | 22 | шт. в перечне | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A− | «direct and indirect subsidiaries and affiliates that conduct substantial business activities in the following countries: Angola, Argentina, Australia, Bangladesh, Brazil, Canada, China, Egypt, Equatorial Guinea, Guyana, Israel, Kazakhstan, Malaysia, Nigeria» | [p. 28](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=28) |
| C08 | Permian: добыча | 1 | MMBOED | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Production in the Permian Basin averaged 1 million barrels of oil-equivalent per day (BOED), a target introduced more than five years ago and delivered on schedule.» | [p. 7](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=7) |
| C09 | Тенгиз (ТШО): добыча после FGP | ≈1 | MMBOED | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Started production at the Future Growth Project and ramped up total production to approximately 1 million barrels of oil-equivalent per day at Tengiz.» | [p. 34](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=34) |
| C10 | Гайана, Stabroek: Yellowtail / Hammerhead | 4-й / 7-й проект | № очереди | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Achieved first oil at Yellowtail, the fourth development in the offshore Stabroek Block, and reached FID on the Hammerhead project, the seventh development.» | [p. 34](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=34) |
| C11 | Leviathan (Израиль): расширение | 2,1 | Bcf/d (мощность) | FID 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Reached FID on the Leviathan Gas Expansion project, which is expected to increase production capacity to 2.1 billion cubic feet per day and support increased exports to Egypt.» | [p. 34](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=34) |
| C12 | Hess: закрытие сделки | июль 2025 | дата | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «In July 2025, the company completed its acquisition of Hess Corporation (Hess).» | [p. 31](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=31) |
| C13 | Angola LNG: доля | 36,4 | % | 31.12.2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Chevron has a 36.4 percent interest in Angola LNG Limited, which processes and liquefies natural gas produced in Angola for delivery to international markets.» | [p. 78](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=78) |
| C14 | СПГ: сбыт equity-СПГ австралийских проектов | н/д (качественно) | — | 2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Most of the equity LNG offtake from the operated Australian LNG projects is committed under binding long-term contracts, with some sold in the Asian spot LNG market.» | [p. 32](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=32) |
| C15 | Добыча природного газа | 8 515 | MMCFD (млн куб. фут/сут) | FY2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Net production of natural gas (Millions of cubic feet per day) 8,515 8,178 7,744» | [p. 22](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=22) |
| C16 | Выручка (sales and other operating revenues) | 184,4 | млрд USD | FY2025 | `SRC-CVX-AR-2025` · 2026-11-22 | A | «Sales and other operating revenues $ 184,432 $ 193,414 $ 196,913» | [p. 21](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=21) |

Примечание к C07: перечень в PDF продолжается — Partitioned Zone between Saudi Arabia and Kuwait, Philippines, Singapore, South Korea, Thailand, United Kingdom, United States, Venezuela; всего 22 позиции (счёт по цитате p. 28).

Примечание к C12: синергии подтверждаются второй цитатой — «Completed the acquisition of Hess, creating a combined company with a premier upstream portfolio and achieving the initial run-rate synergy target of $1 billion», p. 34.

### Chevron — guidance (не факт)

| ID | Показатель | Значение | Ед. | Период | Источник | Ev | Цитата | Стр./URL |
|---|---|---|---|---|---|---|---|---|
| C17 | Прогноз добычи | +7…10 | % к 2025 | 2026 | `SRC-CVX-AR-2025` · 2026-11-22 | G | «The company estimates its net oil-equivalent production in 2026 to increase 7 to 10 percent over 2025, assuming a Brent crude oil price of $60 per barrel and excluding expected asset sales.» | [p. 32](https://www.chevron.com/-/media/chevron/annual-report/2025/documents/2025-AnnualReport.pdf#page=32) |

## 3. TotalEnergies — факты FY2025

| ID | Показатель | Значение | Ед. | Период | Источник | Ev | Цитата | Стр./URL |
|---|---|---|---|---|---|---|---|---|
| T01 | Добыча углеводородов (Company production) | 2 529 | kboe/d (тыс. б.н.э./сут) | FY2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Hydrocarbon production averaged 2,529 thousand barrels of oil equivalent per day in 2025, up nearly 4% year-on-year» | [p. 53](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=53) |
| T02 | Добыча в динамике 2025/2024/2023 | 2 529 / 2 434 / 2 483 | kboe/d | 2023–2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Hydrocarbon production (kboe/d) 2,529 2,434 2,483» | [p. 53](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=53) |
| T03 | Доказанные запасы (combined proved) | 11 218 | Mboe | 31.12.2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «As of December 31, 2025, TotalEnergies' combined proved reserves of oil and gas were 11,218 Mboe (of which 7,054 Mboe were proved developed reserves) compared to 11,073 Mboe» | [p. 76](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=76) |
| T04 | Структура запасов: газ / нефть | 51 / 49 | % | 31.12.2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Natural gas and related products (condensates and natural gas liquids) represent approximately 51% of these reserves, and crude oil 49%.» | [p. 76](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=76) |
| T05 | Продажи СПГ, всего | 43,9 | Mt (млн т) | FY2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Total LNG sales (Mt) 43.9 39.8 44.3» | [p. 93](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=93) |
| T06 | Продажи СПГ из equity-добычи | 15,1 | Mt | FY2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Including sales from equity production(a) 15.1 15.5 15.2» | [p. 93](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=93) |
| T07 | Место на рынке СПГ, портфель, доля | 3-е место; 44; ≈10 | место; Mt; % | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «TotalEnergies is still the world's third-largest player in LNG(3), with a global portfolio of 44 Mt and a global market share of about 10% (4) in 2025.» | [p. 94](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=94) |
| T08 | Продажи СПГ в ЕС+Великобритания; регазификация | 18,4; 19 | Mt; Mt/год | FY2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «TotalEnergies' LNG sales in EU and UK reached 18.4 Mt in 2025 compared to 13.8 Mt in 2024 and to 22.8 Mt in 2023, thanks to 19 Mt/y of regasification capacity.» | [p. 94](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=94) |
| T09 | Majors: добыча в сегменте Integrated LNG | 539 | kboe/d | FY2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Integrated LNG (kboe/d) 539 487 449» | [p. 53](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=53) |
| T10 | Бизнес-сегменты | 5 | шт. (E&P, Integrated LNG, Integrated Power, Refining & Chemicals, Marketing & Services) | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «The reporting structure for the business segments' financial information is based on the following five business segments:» | [p. 466](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=466) |
| T11 | Страны присутствия | ≈120 | стран | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Geographic reach Present in about 120 countries Hydrocarbon exploration and production in about 50 countries» | [p. 13](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=13) |
| T12 | Масштаб (запасы и добыча одной строкой) | 11,2 Bboe; 2 529 kboe/d | Bboe; kboe/d | 31.12.2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Proved reserves of 11.2 Bboe and hydrocarbon production of 2,529 kboe/d» | [p. 13](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=13) |
| T13 | Nigeria LNG: доля и мощность (+T7) | 15; 22; +7,6 | %; Mt/год; Mt/год | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «TotalEnergies holds a 15% interest in Nigeria LNG (NLNG), whose main asset is a liquefaction plant with a total capacity of 22 Mt/y. The project to install an additional 7.6 Mt/y of capacity» | [p. 95](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=95) |
| T14 | Mozambique LNG: доля и мощность | 26,5; 13,1 | %; Mt/год | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «TotalEnergies EP Mozambique Area 1 (TEPMA1) holds a 26.5% interest in the Mozambique LNG project» | [p. 95](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=95) |
| T15 | Angola LNG: доля и мощность | 13,6; 5,2 | %; Mt/год | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «In Angola, TotalEnergies holds a 13.6% interest in Angola LNG (ALNG), which owns a gas liquefaction plant of 5.2 Mt/y capacity, located near Soyo» | [p. 95](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=95) |
| T16 | Yamal LNG: доля и мощность | 20,02; 17,4 | %; Mt/год | 2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «The project includes a four-train gas liquefaction plant with a nominal capacity of 17.4 Mt/y of LNG.» | [p. 70](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=70) |
| T17 | ВИЭ-мощности (gross installed) | 34,1 | GW | 31.12.2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «34.1 GW(1) of gross installed renewable power generation capacities» | [p. 13](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=13) |
| T18 | Персонал | 101 513 | чел. | 31.12.2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «101,513 employees» | [p. 13](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=13) |
| T19 | Продажи (Sales) | 201 | млрд USD | FY2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «Sales: $201 billion» | [p. 13](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=13) |
| T20 | Замещение запасов; обеспеченность запасами | 116; >12 | %; лет | FY2025 / 31.12.2025 | `SRC-TTE-AR-2025` · 2026-11-22 | A | «With a reserve replacement rate of 116% in 2025, TotalEnergies maintains proven reserves life above 12 years» | [p. 51](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=51) |

Примечание к T14: мощность 13,1 Mt/год подтверждается второй цитатой — «two onshore trains with a total capacity of 13.1 Mt/y to liquefy the gas produced by the Golfinho and Atum fields in Offshore Area 1», p. 95.

### TotalEnergies — guidance (не факт)

| ID | Показатель | Значение | Ед. | Период | Источник | Ev | Цитата | Стр./URL |
|---|---|---|---|---|---|---|---|---|
| T21 | Рост добычи | +3 | % | 2026 | `SRC-TTE-AR-2025` · 2026-11-22 | G | «TotalEnergies expects to increase its oil and gas production by 3% in 2026» | [p. 67](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=67) |
| T22 | Добыча в Q1 2026 | >2,6 | Mboe/d | Q1 2026 | `SRC-TTE-AR-2025` · 2026-11-22 | G | «In the first quarter of 2026, hydrocarbon production is expected to be above 2.6 Mboe/d.» | [p. 67](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=67) |
| T23 | Продажи СПГ | >44 | Mt | 2026 | `SRC-TTE-AR-2025` · 2026-11-22 | G | «This growth, combined with LNG sales of over 44 Mt in 2026, should offset the expected decline in LNG prices» | [p. 67](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=67) |
| T24 | Продажи СПГ к 2030 (+50% к 2025) | ≈60 | Mt | 2030 | `SRC-TTE-AR-2025` · 2026-11-22 | G | «LNG sales should thus reach about 60 Mt in 2030.» | [p. 94](https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf#page=94) |

## 4. Сопоставление (только числа из таблиц выше)

| Параметр | Chevron | TotalEnergies |
|---|---|---|
| Добыча FY2025 | 3 723 MBOED (3,7 MMBOED) | 2 529 kboe/d (2,529 Mboe/d) |
| Доказанные запасы на 31.12.2025 | 10 591 MMBOE (10,6 BBOE) | 11 218 Mboe (11,2 Bboe) |
| Замещение запасов | 158 % (FY2025) | 116 % (FY2025); обеспеченность >12 лет |
| Продажи СПГ FY2025 | не раскрываются в Mt | 43,9 Mt (15,1 Mt equity) |
| Сегменты | 2 (Upstream, Downstream) | 5 |
| Страны | 22 юрисдикции «существенной деятельности» | ≈120 стран присутствия; ≈50 стран E&P |
| Выручка/продажи FY2025 | $184,4 млрд | $201 млрд |

Прямое сравнение добычи и запасов корректно только с оговорками по единицам и периметру (см. раздел 5): 1 MBOED = 1 000 kboe/d; Chevron считает газ по 6 000 куб. фут = 1 б.н.э., у TotalEnergies конверсия в kboe/d иная и в цитатах не приведена.

## 5. Ограничения

**1. Guidance ≠ факт.** В отдельные таблицы вынесены: Chevron C17 (+7…10 % добычи в 2026 при Brent $60), TotalEnergies T21–T24 (+3 % добычи в 2026, >2,6 Mboe/d в Q1 2026, >44 Mt СПГ в 2026, ≈60 Mt к 2030, +50 % объёма СПГ 2025–2030). Это заявления компаний о будущем, а не результаты; в моделях их нельзя смешивать с фактическими рядами.

**2. Proved ≠ 2P.** Оба документа раскрывают **объёмы только доказанных** запасов по правилам SEC (1P): Chevron — «net proved», включая консолидированные и аффилированные компании; TotalEnergies — «combined proved reserves», включая пропорциональную долю equity-аффилированных компаний (методика раскрыта в URD, p. 76–77). Объёмов 2P/3P в проверенных разделах нет: у TotalEnergies 2P фигурирует только как reserves life 18 лет по proved+probable (p. 22) и как определение в глоссарии (p. 646); у Chevron probable/possible прямо исключены из расчёта стандартизированной меры (p. 112). Сравнение с 2P-оценками консультантов или российской классификацией (A/B1/C1) напрямую некорректно.

**3. Единицы.** MBOED = тыс. барр. н.э./сут; MMBOED = млн барр. н.э./сут; kboe/d = тыс. б.н.э./сут (1 MBOED = 1 000 kboe/d). Запасы: MMBOE (млн барр. н.э.) и BBOE (млрд барр. н.э.). СПГ: Mt (млн метрических тонн) и Mt/y (мощность в год); 1 Mt СПГ ≈ 1,36 bcm природного газа (плотность зависит от состава, для пересчёта в bcm нужен коэффициент проекта). Chevron пересчитывает газ в нефть по 6 000 куб. фут = 1 барр.; у TotalEnergies коэффициент в kboe/d отдельно не раскрыт в цитируемых фрагментах — поэтому сумма «нефть + газ» двух компаний не аддитивна без пересчёта.

**4. СПГ Chevron: количественного раскрытия в Mt нет.** Полнотекстовый поиск по 123 страницам: `Mtpa` — 0, `Mt/y` — 0, `Mt of LNG` — 0, `million tonnes` — 1 (только про CO2, Gorgon), `liquefaction capacity` — 0. В отчёте 15 упоминаний LNG, все качественные (контракты, рынок, Angola LNG 36,4 %). Поэтому строка C14 — «н/д», а не оценка. Для сопоставимого с TotalEnergies показателя нужен отдельный источник Chevron (например, Supplement to the Annual Report или IR-материалы), в этой сессии не открывался.

**5. Страны считаны по-разному.** Chevron: 22 — это перечень юрисдикций, где ведётся «substantial business activities» (включая Partitioned Zone между Саудовской Аравией и Кувейтом); это не все страны присутствия и не число стран с добычей. TotalEnergies: «about 120 countries» — присутствие, «about 50 countries» — E&P. Прямое сравнение «числа стран» некорректно.

**6. Номера страниц.** Указаны страницы PDF. У Chevron PDF-страница = печатная + 1 (например, PDF p. 21 = печатная 20, PDF p. 22 = печатная 21, PDF p. 33 = печатная 32). У TotalEnergies в проверенных разделах номера совпадают (PDF p. 93 = печатная 93, PDF p. 466 = печатная 466), но в начале документа есть вкладки без номера. Ссылки `#page=N` открывают именно PDF-страницу.

**7. Нормализация цитат.** pypdf извлекает апострофы Chevron как символ-заменитель U+FFFD (например, «Chevron�s»), а надстрочные сноски — как цифры после слова: `reserves2`, `LNG(3)`, `GW(1)`, `production(a)`. В цитатах апострофы/кавычки/дефисы восстановлены до обычных символов, сноски оставлены как в извлечённом тексте. Цитата C14 совпадает с текстом только после склейки переноса «long-\nterm» — в самом PDF дефис стоит в конце строки. Смысл цитат не менялся.

**8. Метаданные PDF.** У файла Chevron поле Title — «2024 Annual Report» (шаблон InDesign), при этом содержание и все цифры — за 2025 год; PDF создан 2026-04-06, 10-K подписан 2026-02-24 и подан в SEC 2026-02-24 (accession 0000093410-26-000078). Файл TotalEnergies пересобран 2026-09-22 (iLovePDF) при том, что URD подан в AMF 2026-03-27; возможны косметические отличия от поданной версии — SHA-256 зафиксирован выше.

**9. Что сверено, а что нет.** Цифры Chevron C01/C03/C05 сверены с SEC 10-K (совпадение полное). Цифры TotalEnergies сверены только внутри URD (перекрёстно: p. 13, 51, 53, 76, 93, 94, 95). Отчётность по сегментам, unit costs, выбросы, 2P-запасы и данные Supplement to the Annual Report не проверялись.

**10. Применение.** Числа пригодны для внешнего факт-пака и для калибровки моделей только со ссылкой на source_id, страницу PDF и дату 2026-11-22. Это не инвестиционная рекомендация и не прогноз.

## 6. Что не открылось / не проверено

- `web_search` недоступен в сессии (нет API-ключа DeepSeek) — поиск вёлся по прямым URL и известным путям публикаций.
- SEC EDGAR через `Invoke-WebRequest` без объявленного User-Agent отдаёт **403**; сработал `curl` с объявленным User-Agent — так получен 10-K для сверки. Полный XBRL-разбор 10-K не делался.
- `chevron.com` без браузерного User-Agent отдаёт **403**; с User-Agent — 200.
- Страница TotalEnergies `/investors/publications-and-regulated-information/universal-registration-document` — **404**; рабочий список — `/regulated-information/annual-financial-reports`, откуда взят PDF URD 2025.
- TotalEnergies Form 20-F 2025 и Chevron Supplement to the Annual Report не открывались.
- 2P/3P-запасы, ресурсы PRMS, данные по выбросам и unit costs в этой сессии не собирались.

## 7. Воспроизведение проверки

```python
from pypdf import PdfReader
r = PdfReader("data/sources/2025_annual_reports/chevron-2025-annual-report.pdf")
t = "\n".join((p.extract_text() or "") for p in r.pages)
assert "3,723" in t and "10,591" in t and "158 percent" in t
```

- Позитивные проверки Chevron: `3,723`, `10,591`, `158 percent`, `two business segments`, `36.4 percent interest in Angola LNG`.
- Негативные проверки Chevron (СПГ в Mt): `Mtpa`, `Mt/y`, `Mt of LNG`, `liquefaction capacity` — 0 совпадений; `million tonnes` — 1 (CO2, Gorgon).
- Позитивные проверки TotalEnergies: `2,529 thousand barrels`, `11,218 Mboe`, `43.9`, `five business segments`, `about 120 countries`, `22 Mt/y`, `13.1 Mt/y`, `5.2 Mt/y`, `17.4 Mt/y`.
- Цитаты в таблицах проверены на вхождение в текст соответствующей PDF-страницы (скрипт сверки: 41 строка фактов, расхождение только у T18 — «101,513 employees» встречается и на p. 6, и на p. 13; в таблице указана p. 13, где есть контекст).

