# 228. Commercial Strategy Agent

Дата: 2026-11-23. Контур: `B2B/B2G · RESEARCH_ONLY · admission=false`. Агент помогает начать
коммерческую работу без скрытого таргетинга и без автоматической рассылки.

## 1. Назначение

`commercial-strategist` превращает продуктовый scope в оффер: buyer-организация, роль-центр,
вопрос discovery, deliverables, фиксированная цена, срок, допущения, исключения, procurement path.

Это **не** агент влияния и не база контактов. «Кто принимает решение» задаётся как публичная роль
(например, Director of Procurement, Head of Market Risk), а не как частное лицо с email/телефоном.

## 2. Цена — наша цена, не цена клиента

25 офферов в `marketing/commercial_offers_25/` содержат фиксированные AMFE list prices в USD
за конкретный scope. Это не конкурентный benchmark, не оценка бюджета покупателя и не обещание ROI.
Налоги и внешние данные отдельно. Все предложения — `HYPOTHESIS`, `not_client=true`, `sent=false`.

Базовые полосы — из `marketing/pricing.md`: 7 500–10 000 (2–4 недели), 15 000–25 000 (4–6),
20 000–30 000 (8–12), 25 000–80 000 (API/6–12). Для оффера зафиксирована одна list price.

## 3. Навыки агента

- ICP по типу организации/мандату, не по частной персоне;
- mapping buyer center по ролям;
- B2B/B2G procurement qualification (RFI/RFP/vendor onboarding);
- fixed-scope offer design и согласование с price book;
- нейтральные discovery-вопросы и метрики успеха пилота;
- lawful OSINT handoff, legal handoff, archivist context pack;
- CRM hygiene; никаких отправок без Lead/human approval.

## 4. Стыковка с остальными агентами

| Задача | Владелец |
|---|---|
| Публичный факт и source log | `osint-scout` |
| Гated source request (G2/G3) | `osint-scout` оформляет; Lead/человек авторизует |
| Provenance и контекст-пак | `archivist` |
| Закон/контракт/юрисдикция | `international-counsel`; подпись `human-lawyer` |
| Фактическое решение о цене/адресате/отправке | Lead/human |
| Независимая проверка evidence | `evidence-qa` |

При противоречии агент не «переписывает» источник: сохраняет обе позиции с provenance и передаёт
на Evidence QA/Lead.

## 5. Красные линии

- ни скрытые сети, ни «закулиса», ни covert influence;
- ни частные контакты/профили, ни scraping персональных данных;
- ни закрытые базы без лицензии/письменного разрешения;
- ни отправка писем, submit тендеров или переговоры от имени Flumos;
- ни выдуманные клиенты, бюджеты, ROI, точность или admitted status.

## 6. Использование

```powershell
python tools/commercial_strategy.py --check
python tools/commercial_offers.py --check
python tools/commercial_strategy.py --role commercial-strategist `
  --question "Stress-test one energy procurement decision" `
  --buyer-type "public utility" --jurisdiction US --offer-id C25-007 --json
python -m unittest tests.test_commercial_strategy tests.test_commercial_offers
```

Brief автоматически помечен `HYPOTHESIS`, `sent=false`, `outreach_authorized=false`.
