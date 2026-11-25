# 235. Simulator: live smoke через HTTP (2026-11-25)

Дата: 2026-11-25. Роль: M1 (docs/207). Дополняет [docs/231](231_Simulator_демо_сценарий.md):
231 проверяет демо **в процессе** (`SimulatorApp.run`), этот документ — **живой HTTP-сервер**
(`tools/simulator_ui.py`, [docs/192](192_Simulator_UI.md)).

Результат: **smoke PASS** локально. Production-деплой, TLS и прокси **не проверялись**.

## 1. Команда

```powershell
python tools/simulator_ui.py --host 127.0.0.1 --port 8095 --quiet
```

Сервер поднят локально; после проверок остановлен. Ничего не публиковалось наружу.

## 2. Что проверено

| Проверка | Результат |
|---|---|
| `GET /api/health` | `status=RESEARCH_ONLY`, `admission=false`, `version=1.2.0`, 6 сценариев |
| health.checks | `aggregate_only`, `deterministic`, `oil_module`, `gas_module`, `geopolitics_artifact`, `institutions_module`, `technology_module` — все `true` |
| `POST /api/run` для 6 сценариев | **6/6 OK**, каждый `RESEARCH_ONLY`, `admission=false`, `causal_claim=false`, есть `digest` |
| Детерминизм по HTTP | два одинаковых запроса → один и тот же `digest` |
| `GET /` | 200, форма есть, дисклеймер «не прогноз» присутствует |
| Заголовки | CSP `default-src 'none'… frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` |
| `GET /api/scenarios` | 6 сценариев |
| `GET /api/run` | отклонён (400) — метод только POST |

Карточки по сценариям: `hormuz_base` 6, `hormuz_stress` 9, `gas_shock` 6, `geopolitics` 6,
`institutions` 6, `technology` 6.

## 3. Что это закрывает и чего не закрывает

**Закрывает** пункт M1 «демо-сценарий» в части «работает не только в процессе, но и по HTTP»:
сервер стартует, отдаёт контракт, считает все шесть сценариев, детерминирован, отдаёт
security-заголовки, отклоняет неверный метод.

**Не закрывает** и не заявляет:

1. **Деплой** — это localhost, не staging/prod; TLS, reverse proxy, WAF отсутствуют.
2. **Нагрузку** — один поток на запрос; конкурентность/латентность не измерялись.
3. **Auth/квоты** — в smoke режим без ключей; режим `--require-auth` покрыт тестами
   ([docs/192](192_Simulator_UI.md) §7), но в этом прогоне не включался.
4. **Геополитику** — проверено на объявленной синтетической фикстуре; при наличии
   `build/geopolitics/report.json` числа будут другими, контракт тот же.
5. **SLA** — не измерялся и не обещается.

## 4. Следующий шаг

- Если нужен внешний показ — только через reverse proxy с TLS и ключами (`--require-auth`),
  см. [docs/183](183_Forecast_API_шлюз.md); это решение человека, не автоматический деплой.
- Для пилота — шлюз, а не этот демо-сервер ([docs/192](192_Simulator_UI.md) §8.7).

## 5. Статусы

`RESEARCH_ONLY`, `admission=false`, `causal_claim=false`; C6 по-прежнему **0/12** и **1/4**.
Никаких утверждений о точности, вероятности или допуске модели.
