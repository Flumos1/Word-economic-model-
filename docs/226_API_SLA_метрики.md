# 226. API SLA, health и метрики (M1)

Дата: 2026-11-22. Статус: **RESEARCH_ONLY, admission=false**. Это SLA-заготовка для демо, а не
договорной SLA с клиентом.

## 1. Что добавлено

Публичные endpoints без ключа:

| Endpoint | Что отдаёт |
|---|---|
| `GET /v1/health` | liveness/readiness: feed, API keys, product feed; `OK`/`DEGRADED`/`BLOCKED` |
| `GET /v1/metrics` | requests, errors, error rate, latency p50/p95/max, by path, status counts, uptime |
| `GET /v1/sla` | объявленные цели + наблюдаемые значения; `contractual=false` |
| `GET /v1/status` | прежний статус, теперь со ссылками на health/metrics/sla |
| `GET /openapi.json` | спецификация, включая новые endpoints |

Метрики — in-process, без внешних зависимостей и без секретов; не содержат путей к raw data,
ключей и тел запросов.

## 2. Объявленные цели (не договор)

| Метрика | Цель |
|---|---|
| availability | 99,5 % |
| latency p95 | ≤ 500 ms |
| error rate | ≤ 1 % |
| window | process lifetime |

`/v1/sla` возвращает `declared.contractual=false` и пояснение: договорной SLA фиксируется отдельно
с клиентом. Это защита от ложного обещания.

## 3. Как проверить

```powershell
# запуск локально (ключи обязательны для /v1/modules и /v1/forecast)
$env:AMFE_API_KEYS = "demo_key:free"
python tools/forecast_api_server.py --api-dir build/api --product build/product/forecast_latest.json
# в другом терминале:
curl http://127.0.0.1:8080/v1/health
curl http://127.0.0.1:8080/v1/metrics
curl http://127.0.0.1:8080/v1/sla
curl http://127.0.0.1:8080/v1/status
```

Тесты: `python -m unittest tests.test_api_sla` (9 тестов) + `tests.test_forecast_api_server` (39).

## 4. Runbook (демо)

1. Проверить `/v1/health` → `OK`; если `BLOCKED` — проверить ключи/фид.
2. Проверить `/v1/status` → `admission=false`, статусы модулей.
3. Проверить `/v1/metrics` → `requests_total` растёт, `error_rate` в норме.
4. Проверить `/v1/sla` → `declared.contractual=false`.
5. Сделать smoke: `/v1/modules`, `/v1/scenarios`, `/v1/forecast?target=oil_brent&horizon=6`.
6. Записать `X-Request-Id` для инцидента; ключи не логировать.

## 5. Ограничения

- Метрики живут в памяти процесса: рестарт обнуляет; нет Prometheus/Grafana/внешнего мониторинга.
- Нет алертов, SLO-бюджетов, on-call, распределённого трейсинга.
- `/v1/health` не проверяет внешние зависимости, только локальный фид/ключи.
- Нет TLS/прокси: для демо — локально/за шлюзом; прод — отдельная задача.
- SLA не договорной; для клиента — отдельный документ и подпись.

## 6. Следующее

1. Prometheus-совместимый `/metrics` (text) и scrape-конфиг.
2. Алерты: error rate > 1 %, p95 > 500 ms, health != OK.
3. Smoke-скрипт в CI, который поднимает сервер на свободном порту и проверяет endpoints.
4. Договорной SLA-шаблон — после первого пилота.
