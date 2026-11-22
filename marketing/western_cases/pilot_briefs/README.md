# Pilot briefs: Equinor, Shell, ConocoPhillips

Дата: 2026-11-22. Статус: **DRAFT · RESEARCH_ONLY · admission=false · not_client=true**. Ничего не
отправлено.

## Что это

Три англоязычных one-pager'а под первых западных пилотов: по одной странице A4, каждый в двух
форматах — Markdown (источник) и PDF (для отправки после проверки). Это шаг из
[`docs/214`](../../../docs/214_50_западных_кейсов.md) («добавить англоязычные one-pager'ы под
конкретные компании») на базе факт-паков [`docs/219`](../../../docs/219_Conoco_факты.md) и
[`docs/220`](../../../docs/220_Shell_Equinor_факты.md).

## Файлы

| Файл | Кейсы | О чём |
|---|---|---|
| `equinor.md` / `equinor.pdf` | `wc-101`, `wc-102` | портфель проектов при шоке нефти и газа; TCO шельфовых поставщиков |
| `shell.md` / `shell.pdf` | `wc-005`, `wc-006` | фрахт и цена в одном бюджетном контуре; API с версией и хешем |
| `conocophillips.md` / `conocophillips.pdf` | `wc-011`, `wc-012` | темп бурения при диапазоне цены; TCO трубной продукции |
| `build_pilot_briefs.py` | — | сборка PDF из Markdown |
| `README.md` | — | этот файл |

## Источники и числа

- `marketing/western_cases/cases.json` — единственный источник id кейсов, цен, сроков и фактов.
  Факты продублированы в `docs/219` (ConocoPhillips) и `docs/220` (Shell, Equinor).
- Числа перенесены **дословно**: запятая как десятичный разделитель (`28,4 Mt`), пробел как
  разделитель тысяч (`2 137 mboe/d`). Единственная правка — единица `BCM/год` → `BCM/year`
  (Hammerfest LNG), чтобы текст был полностью английским; цифры не менялись.
- В каждом brief'е 5 фактов с `source_id`: `SRC-EQNR-AR-2025`, `SRC-SHELL-AR-2025`,
  `SRC-COP-AR-2025`, `SRC-COP-OPS-2026`. Evidence `A`, дата обращения 2026-11-21.
- Строка «Both cases» — арифметика двух кейсов (цена и срок складываются). Других расчётов в
  brief'ах нет; все числа проверяются тестом на совпадение с `cases.json`.

## Структура brief'а

1. **Goal** — какое решение закрывает пилот.
2. **Two pilot cases** — вопрос и результат по каждому кейсу.
3. **Public facts and sources** — 5 фактов с `source_id`.
4. **Price and timing** — цена и срок по кейсам и по паре.
5. **What we need from you** — данные со стороны заказчика (можно обезличить).
6. **Limits and status** — RESEARCH_ONLY, admission=false, not_client=true, гипотеза, не прогноз.
7. Контакт `flumos@gmail.com` (`mailto:`) в конце.

## Правила, которые держим

- **Нет контактов компаний**: ни ФИО, ни телефонов, ни адресов, ни ссылок на чужие сайты.
  Единственный адрес в тексте — `flumos@gmail.com`.
- **Компания не называется клиентом**: слова «client» в brief'ах нет нигде, кроме обязательного
  флага `not_client=true`. Поэтому модуль «API/Client Pack» из `wc-006` описан как
  «API integration» и «a delivery pack (PDF, XLSX, JSON, HTML)»; id кейса и числа сохранены.
- **Нет обещаний точности**: вместо «accurate/precise» — «ranges with stated assumptions and a
  reproducible calculation». Нет «admitted model», «guaranteed saving», «forecast».
- **Флаги на месте**: `RESEARCH_ONLY · admission=false · not_client=true` в шапке, в разделе 6 и
  в футере каждой страницы PDF.
- **Нет выручки и численности** (правило `no_revenue_or_headcount` из `cases.json`).
- Цены — только USD, без НДС; полосы и логика — в [`marketing/pricing.md`](../../pricing.md).

## Сборка

```bash
python marketing/western_cases/pilot_briefs/build_pilot_briefs.py
python marketing/western_cases/pilot_briefs/build_pilot_briefs.py --only shell
python marketing/western_cases/pilot_briefs/build_pilot_briefs.py --check
```

Нужны `python-docx` и LibreOffice (`C:\Program Files\LibreOffice\program\soffice.exe`). PDF не
байт-детерминирован (метаданные LibreOffice), контент детерминирован: тот же Markdown даёт тот же
текст. Сборщик **fail-closed**: если PDF вышел не на одну страницу, он уменьшает кегль по шагам, а
если не помогло — падает с ошибкой и не пишет файл.

## Тесты

```bash
python -m pytest tests/test_pilot_briefs.py -q
```

Проверяют: наличие файлов, одну страницу A4, число фактов и `source_id`, совпадение цен, сроков и
фактов с `cases.json`, сумму по паре кейсов, отсутствие контактов и слова «client», дисклеймеры в
Markdown и в PDF, ссылку `mailto:`, содержание README.

## Ограничения

- Факты взяты из годовых отчётов за 2025 год и **не сверены человеком** по страницам PDF; перед
  отправкой нужна проверка (см. `docs/219` §5 и `docs/220` §6).
- Brief — гипотеза о боли, а не утверждение о компании; ни один пилот не согласован, ни одна цена
  не подтверждена заказчиком.
- Не отправлять без проверки адресата и решения владельца проекта.
