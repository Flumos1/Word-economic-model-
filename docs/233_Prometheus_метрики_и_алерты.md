# 233. Prometheus-метрики и алерты Forecast Gateway (M1)

Дата: 2026-11-25. Статус: **RESEARCH_ONLY, admission=false**. Это операционный контракт для
демонстрационного Forecast Gateway, **а не SLA-гарантия** и не обещание доступности, точности или
ROI. Метрики in-process: они показывают, что видел сам процесс, и обнуляются при рестарте.

Связанные документы: [docs/226](226_API_SLA_метрики.md) (JSON `/v1/metrics`, `/v1/sla`),
[docs/183](183_Forecast_API_шлюз.md) (шлюз Forecast API), [docs/230](230_M1_ClientPack_Simulator_readiness.md)
(M1 readiness). Изменённый сервер: `tools/forecast_api_server.py`; правила алертов:
`deploy/prometheus/amfe_alerts.yml`.

## 1. Контракт endpoint'а

| Свойство | Значение |
|---|---|
| Метод/путь | `GET /v1/metrics/prometheus` |
| Аутентификация | **не требуется** (как `/v1/health`, `/v1/metrics`, `/v1/sla`) |
| `Content-Type` | `text/plain; version=0.0.4; charset=utf-8` |
| Формат | Prometheus text exposition format 0.0.4: `# HELP`, `# TYPE`, затем `name{labels} value` |
| Кодировка/переводы строк | UTF-8, LF, ответ заканчивается `\n` |
| Тело | только агрегаты in-process метрик; без ключей, тел запросов, сырых данных, абсолютных путей и внешних URL |
| Побочный эффект | сам запрос не входит в собственный снимок (снимок берётся до записи метрики текущего запроса) |

JSON-версия тех же чисел — `GET /v1/metrics`; prometheus-версия строится из того же снимка
`GatewayMetrics.snapshot()`, поэтому `requests_total`, `errors_total`, `error_rate`, p50/p95
согласованы (с точностью до запроса `/v1/metrics`, который успел попасть в следующий снимок).

## 2. Пример scrape config

```yaml
# prometheus.yml — только локальный пример; внешних адресов и выдуманных URL здесь нет.
global:
  scrape_interval: 30s
  evaluation_interval: 30s

rule_files:
  - "amfe_alerts.yml"   # этот репозиторий: deploy/prometheus/amfe_alerts.yml

scrape_configs:
  - job_name: amfe-forecast-gateway
    metrics_path: /v1/metrics/prometheus
    scheme: http
    static_configs:
      - targets: ["127.0.0.1:8080"]   # локальный запуск: python tools/forecast_api_server.py
```

Запуск шлюза для локального scrape (ключи для защищённых маршрутов обязательны, но
`/v1/metrics/prometheus` публичный):

```powershell
$env:AMFE_API_KEYS = "demo_internal_key_0001:internal"
python tools/forecast_api_server.py --host 127.0.0.1 --port 8080
# проверка:
curl.exe -s http://127.0.0.1:8080/v1/metrics/prometheus
```

Проверка файла правил (если установлен Prometheus CLI):
`promtool check rules deploy/prometheus/amfe_alerts.yml`.

## 3. Метрики

| Метрика | Тип | Метки | Смысл |
|---|---|---|---|
| `amfe_gateway_up` | gauge | — | `1`, пока процесс отвечает на scrape (0 в алерте = процесс/таргет недоступен) |
| `amfe_uptime_seconds` | gauge | — | секунды с запуска процесса (monotonic-часы процесса, не wall-clock) |
| `amfe_requests_total` | counter | — | все HTTP-запросы с момента запуска |
| `amfe_errors_total` | counter | — | ответы со статусом ≥ 400 |
| `amfe_error_rate` | gauge | — | `errors_total / requests_total` (0 при нуле запросов), lifetime |
| `amfe_request_duration_p50_ms` | gauge | — | медиана длительности запросов, мс, lifetime |
| `amfe_request_duration_p95_ms` | gauge | — | 95-й перцентиль длительности, мс, lifetime |
| `amfe_http_status_total` | counter | `status="200"`, `status="404"`, … | ответы по HTTP-коду |
| `amfe_requests_by_path_total` | counter | `path="/v1/health"`, … | запросы по известным путям API |

Все метрики имеют `# HELP` и `# TYPE` (это проверяется тестами). Других метрик endpoint не
отдаёт: нет гистограмм, `quantile` и «информационных» gauge с версиями, чтобы не создавать
впечатление большего, чем есть.

### 3.1 Кардинальность `path`

`amfe_requests_by_path_total` получает label только для известных путей API:

`/openapi.json`, `/v1`, `/v1/status`, `/v1/health`, `/v1/metrics`, `/v1/metrics/prometheus`,
`/v1/sla`, `/v1/modules`, `/v1/scenarios`, `/v1/scenarios/{scenario_id}`, `/v1/forecast`.

Документы сценариев (`/v1/scenarios/<id>`) схлопываются в один шаблонный label
`path="/v1/scenarios/{scenario_id}"` (сырой id не попадает в метрику); все остальные пути, включая
404/401 и query-строки (query отбрасывается ещё при записи метрики), схлопываются в
`path="other"`. Поэтому число series ограничено числом известных путей + 1 и не растёт от клиентских
id/URL.

### 3.2 Экранирование label value

Перед выводом значения меток экранируются: `\` → `\\`, перевод строки → `\n`, двойная кавычка →
`\"` (функция `prometheus_escape_label`). Это защищает exposition от поломки при нестандартном
входном пути; сырые значения в вывод не попадают (кардинальность также это гарантирует).

## 4. Правила алертов

Файл: `deploy/prometheus/amfe_alerts.yml`, группа `amfe-forecast-gateway`, 4 правила:

| Alert | Выражение | `for` | `severity` | Зачем |
|---|---|---|---|---|
| `AmfeGatewayDown` | `amfe_gateway_up == 0` | `2m` | critical | процесс не отвечает на scrape |
| `AmfeHighErrorRate` | `amfe_error_rate > 0.05` | `5m` | warning | >5% ответов со статусом ≥ 400 за lifetime |
| `AmfeHighLatencyP95` | `amfe_request_duration_p95_ms > 500` | `5m` | warning | p95 > 500 мс (демо-порог) |
| `AmfeNoTraffic` | `increase(amfe_requests_total[10m]) == 0` | `10m` | warning | нет запросов 10 минут (idle-демо или не тот таргет) |

У каждого правила есть `labels.severity`, `labels.service`, `annotations.summary` и
`annotations.description`. Runbook-URL намеренно нет (не выдумываем ссылки); порядок действий —
в docs/226 §4 и в описании алерта. Пороговые значения 5%/500 мс — демонстрационные и **не**
договорные SLO.

## 5. Ограничения (честно)

- **In-process метрики**: живут в памяти процесса; рестарт обнуляет счётчики и uptime; несколько
  процессов/реплик не агрегируются (Prometheus увидит разные series с разными инстансами, если
  добавить label `instance`).
- **Нет гистограмм и summary**: p50/p95 — это перцентили **по lifetime-выборке** последних ≤4096
  наблюдений, а не PromQL `histogram_quantile` и не перцентили за окно. Для windowed-метрик нужна
  отдельная гистограмма — не входит в M1.
- **`amfe_error_rate` lifetime**, а не по окну: после всплеска ошибок значение снижается медленно.
- **Публичный endpoint**: `/v1/metrics/prometheus` без ключа (как health/metrics/sla). Это
  сделано для локального scrape; при публикации наружу — только через локальный/приватный
  интерфейс или reverse proxy с ограничением доступа. TLS/аутентификация scrape — вне M1.
- **Правила алертов — файл, а не развёрнутый мониторинг**: в репозитории нет Prometheus/Grafana,
  Alertmanager, on-call и SLO-бюджетов. Подключение — задача эксплуатации.
- **Это не SLA**: ни availability, ни latency, ни error-rate числа не являются договорным
  обязательством; `admission=false`, статус RESEARCH_ONLY. Нельзя цитировать их как SLA/ROI/
  гарантию качества прогноза.
- **Нет внешних проверок**: `amfe_gateway_up=1` означает «процесс отвечает на запрос», а не
  «фид/модель качественны»; `/v1/health` проверяет локальный фид и ключи, а не внешние зависимости.

## 6. Проверка

```powershell
# обязательный набор (все тесты должны быть OK):
python -m unittest tests.test_api_prometheus tests.test_forecast_api_server tests.test_api_sla

# быстрый ручной smoke:
$env:AMFE_API_KEYS = "demo_internal_key_0001:internal"
python tools/forecast_api_server.py --host 127.0.0.1 --port 8080
curl.exe -s http://127.0.0.1:8080/v1/metrics/prometheus
curl.exe -s http://127.0.0.1:8080/v1/metrics
```

Тесты `tests/test_api_prometheus.py` (22 шт.) проверяют Content-Type, HELP/TYPE для каждой метрики,
рост счётчиков, согласованность с JSON `/v1/metrics`, пустые метрики на старте, экранирование
меток, отсутствие ключей/сырых путей/URL, кардинальность `path` и неразглашение scenario id,
доступность без ключа, OpenAPI/status-описание, живой HTTP-ответ, а также наличие 4 правил
алертов с нужными `expr`/`for`/`severity`/`annotations` и отсутствие выдуманных URL.

## 7. Остаток M1 (вне этой задачи)

1. Подключить scrape/алерты в реальном контуре эксплуатации (Prometheus + Alertmanager + канал
   уведомлений) — инфраструктурная задача, не меняющая шлюз.
2. При необходимости — гистограмма длительности и windowed error rate (отдельный контракт).
3. Публикация шлюза наружу: TLS, reverse proxy, ограничение доступа к `/v1/metrics/prometheus` на
   уровне сети; сейчас endpoint публичный по дизайну demo-контура.
