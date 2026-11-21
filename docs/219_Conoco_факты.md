# 219. ConocoPhillips: числа для кейсов (открытые источники)

Дата: 2026-11-21. Статус: **RESEARCH_ONLY, admission=false, not_client=true**. Это факт-пак для
двух кейсов (`wc-011`, `wc-012`), а не клиентские данные и не прогноз. Все числа — из публичных
первоисточников; ссылки и даты обращения указаны.

## 1. Источники

| ID | Источник | URL | Дата |
|---|---|---|---|
| SRC-COP-AR-2025 | ConocoPhillips 2025 Annual Report / Form 10-K (PDF, 165 стр.) | https://static.conocophillips.com/files/resources/2025-conocophillips-annual-report.pdf | 2026-11-21 |
| SRC-COP-OPS-2026 | ConocoPhillips «Where we operate» | https://www.conocophillips.com/operations/ | 2026-11-21 |
| SRC-COP-WHO | ConocoPhillips «Who we are» | https://www.conocophillips.com/about-us/who-we-are/ | 2026-11-21 |

## 2. Факты

| # | Факт | Значение | Источник | Evidence | Примечание |
|---|---|---|---|---|---|
| F1 | Добыча за 2025 | **2 375 MBOED** | SRC-COP-AR-2025 | A | total company production; +388 MBOED / +20 % к 2024 |
| F2 | Добыча 2026, guidance | **2,33–2,36 MMBOED** | SRC-COP-AR-2025 | A | 2026 outlook; Q1 2026: 2,30–2,34 |
| F3 | CAPEX 2026, guidance | **~$12B** | SRC-COP-AR-2025 | A | 2026 capital expenditures |
| F4 | Доказанные разработанные запасы (consolidated), конец 2025 | **4,2 BBOE** | SRC-COP-AR-2025 | A | было 4,5 BBOE на конец 2024 |
| F5 | Стран с операциями на 30.06.2026 | **15** | SRC-COP-OPS-2026 | A | на сайте «Where we operate» |
| F6 | Операционных сегментов в 2026 | **6** | SRC-COP-OPS-2026 | A | Lower 48; Europe, Middle East & North Africa; Asia Pacific; Alaska; Canada; Other International |
| F7 | Лицензировано LNG-трактов по технологии Optimized Cascade | **28** | SRC-COP-AR-2025 | A | «licensed for use in 28 LNG trains around the world» |
| F8 | СПГ-оффтейк в Северной Америке | **10,2 MTPA** | SRC-COP-AR-2025 | A | offtake commencing 2026–2031 |
| F9 | Регазификация в Европе | **~6,7 MTPA** | SRC-COP-AR-2025 | A | total regasification capacity |
| F10 | PALNG Phase 1 offtake | **5 MTPA** | SRC-COP-AR-2025 | A | initial offtake; total commercial portfolio 10 MTPA |
| F11 | Доля в Port Arthur LNG Phase 1 | **30 %** | SRC-COP-AR-2025 | A | startup scheduled 2027 |

## 3. Что обновлено в кейсах

- **`wc-011`** (upstream, бюджет/бурение): факты F1–F6 — добыча, guidance, CAPEX, запасы, страны,
  сегменты. Вопрос кейса теперь опирается на реальный масштаб: «какой темп бурения выдержит бюджет
  при Brent 139,04–268,52 USD/bbl и CAPEX ~$12B».
- **`wc-012`** (upstream, закупки/TCO): факты F7–F10 — 28 трактов, 10,2 MTPA, 6,7 MTPA, 5 MTPA;
  это масштаб портфеля, для которого считается TCO, а не выручка компании.
- `evidence_status` кейсов остаётся `HYPOTHESIS`: числа подтверждены, но **боль компании** ещё не
  подтверждена; `not_client=true`.

## 4. Что важно не перепутать

1. **F4 — доказанные разработанные (proved developed), consolidated**, не все proved reserves.
2. **Шесть сегментов — на 2026 год**; в 10-K за 2025 год написано «five operating segments».
   Это разные даты, не противоречие; в кейсе помечено «в 2026».
3. **F3 — CAPEX guidance**, а не фактический CAPEX и не выручка.
4. Числа из годового отчёта — за 2025 год; для 2026 — только guidance, он может измениться.
5. Мы не добавляли контакты, ФИО, телефоны и не называли компанию клиентом.

## 5. Следующее

- Проверить факты человеком по PDF (страницы/сноски) и поднять evidence фактов до `A+` при необходимости.
- Добавить такие факт-паки для Shell/Equinor, когда их S1-страницы извлекутся.
- Не отправлять письма до проверки адресатов.
