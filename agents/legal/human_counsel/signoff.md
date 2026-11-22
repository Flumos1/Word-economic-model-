# Signoff: подпись юридического заключения

Подписывает только реальный лицензированный юрист. AI готовит проект и источники, но не ставит
подпись.

## Обязательные поля

```json
{
  "schema": "amfe.legal.signoff.v1",
  "matter_id": "LEG-2026-001",
  "jurisdiction": "DE",
  "counsel_id": "HC-DE-001",
  "license": "BAR-DE-...",
  "good_standing_verified_at": "2026-11-22",
  "verified_by": "human:...",
  "scope": "…",
  "assumptions": ["…"],
  "limitations": ["…"],
  "signed_at": "2026-11-22",
  "ai_research_memo": true
}
```

## Запреты

- `signed_by` = AI/agent/model — отказ;
- отсутствие лицензии/юрисдикции/scope — отказ;
- `verified_by` не `human:*` — отказ;
- подпись по юрисдикции со статусом `UNASSIGNED` — отказ;
- «гарантируем результат», «admitted», «окончательно» — отказ.

## Формат заключения

Вопрос → факты → применимое право → анализ → риски → опции → оговорки → подпись.
