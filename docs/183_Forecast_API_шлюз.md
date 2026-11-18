# 183. Живой Forecast API-шлюз (UC-45): ключи, квоты, коды и fail-closed

Дата: 2026-11-18. Задача: task-65 (UC-45 «API-подписка на прогнозы/сценарии», P0/26).
Статус: **RESEARCH_ONLY** — шлюз не меняет статусы моделей и ничего не допускает
(`admission=false`), это транспорт поверх уже опубликованного контракта.

Инструмент: [tools/forecast_api_server.py](../tools/forecast_api_server.py) (только stdlib:
`http.server`, без Flask/FastAPI). Тесты:
[tests/test_forecast_api_server.py](../tests/test_forecast_api_server.py) (**38** тестов).
Схема ответов: `amfe.forecast_api.live.v1`, версия шлюза `1.0.0`.

Основание и источники: [docs/156](156_Forecast_API_и_рыночный_пакет.md) (контракт
`amfe.forecast_api.v2`), [docs/161](161_Реестр_API_паспорта.md) (паспорта модулей),
[docs/157](157_Реестр_модулей.md) (реестр), [docs/174](174_50_кейсов_использования.md) (UC-45:
«API статический, нет живого шлюза/ключей/SLA»).

## 0. Что это и чего это не касается

**Это** HTTP-шлюз на stdlib, который отдаёт ровно те документы, что уже лежат в `build/api/`
(контракт v2) и `build/product/forecast_latest.json` (продуктовый фид): статус, паспорта модулей,
сценарии, forecast-конверт и OpenAPI. Он добавляет то, чего у статического контракта не было:
аутентификацию по ключам, квоты/лимиты, коды ответов, CORS, версии и явный fail-closed.

**Это не** модель и не пересчёт: ни одно число не вычисляется в шлюзе. Числа — из `MarketResult`
(нефтяной сценарий) и из продуктового фида; шлюз их только переупаковывает и проверяет политику.
`build/api/`, `src/`, `protocols/`, frozen-файлы **не изменялись**; артефакты шлюза в git не
коммитятся.

## 1. Запуск

```powershell
# 1) ключи (обязательно; без них сервис отвечает 503, а не «пускает всех»)
$env:AMFE_API_KEYS = "demo_free_key_01:free,demo_pro_key_01:pro,ops_key_01:internal"

# 2) проверка конфигурации и фидов без запуска сервера
python tools/forecast_api_server.py --check

# 3) сам шлюз
python tools/forecast_api_server.py --host 127.0.0.1 --port 8080
# stdout: {"status": "OK", "url": "http://127.0.0.1:8080", "schema": "amfe.forecast_api.live.v1", ...}
```

Параметры CLI:

| Флаг | По умолчанию | Назначение |
|---|---|---|
| `--host` | `127.0.0.1` | адрес привязки (наружу — только за reverse proxy) |
| `--port` | `8080` | порт; `0` — свободный порт (печатается фактический) |
| `--api-dir` | `build/api` | каталог статического контракта |
| `--product-feed` | `build/product/forecast_latest.json` | продуктовый фид (таргет `cpi_yoy`) |
| `--cors-origin` | `*` (или `AMFE_API_CORS_ORIGIN`) | значение `Access-Control-Allow-Origin` |
| `--allow-point` | выключено | отдавать точечную оценку продуктового фида (по умолчанию скрыта) |
| `--check` | — | диагностика (JSON в stdout), сервер не поднимается |
| `--quiet` | — | только предупреждения в лог |
| `--version` | — | версия шлюза |

Переменные окружения: `AMFE_API_KEYS` (ключи), `AMFE_API_CORS_ORIGIN` (CORS),
`AMFE_API_ALLOW_POINT` (`1/true/yes/on` — включить точечную оценку продуктового фида).

Коды возврата процесса: `0` — работает / `--check` дал `OK` или `DEGRADED`; `2` — неверная
конфигурация (например, ключ короче 8 символов или неизвестный тариф) либо `--check` дал
`BLOCKED`. Неверная конфигурация — отказ стартовать, а не «тихий» дефолт.

Фиды читаются **один раз при старте** (снимок): после пересборки `build/api` шлюз нужно
перезапустить. Это осознанно: ответы детерминированы и не «дрожат» между запросами.

## 2. Ключи, тарифы и квоты

`AMFE_API_KEYS` — список элементов `ключ[:тариф]` через запятую, точку с запятой или пробел.
Без тарифа — `free`. Ключ короче 8 символов, неизвестный тариф и дубликат ключа отклоняются
(`ApiConfigError`, exit 2).

Внутри процесса хранятся **только SHA-256 дайджесты** ключей; сравнение —
`hmac.compare_digest`. Плейнтекст ключа не пишется ни в ответы, ни в логи, ни в диагностику.
В URL ключ не принимается: только `X-API-Key: <key>` или `Authorization: Bearer <key>`
(ключ в query-строке неизбежно утекает в access-логи и прокси).

| Тариф | Запросов/минуту | Запросов/сутки |
|---|---|---|
| `free` | 10 | 200 |
| `pro` | 60 | 5 000 |
| `internal` | 600 | 200 000 |

Тарифы — константа `PLANS` в коде (менять осознанно и вместе с тестами). Квоты — фиксированные
окна в памяти процесса: минутное окно и суточное (UTC). Ответы несут заголовки
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (секунд до сброса),
`X-RateLimit-Day-Limit`, `X-RateLimit-Day-Remaining`, `X-Plan`; при `429` — ещё и `Retry-After`.
Ключи независимы: исчерпание квоты одного не блокирует другие.

## 3. Эндпоинты

| Метод | Путь | Ключ | Что отдаёт |
|---|---|---|---|
| GET | `/openapi.json` | нет | OpenAPI 3.0.3 самого шлюза (не зависит от фидов) |
| GET | `/v1/status` | нет | статус, версии, фиды (schema/generated_at/SHA-256), таргеты, лимиты, checks |
| GET | `/v1/modules` | да | паспорта 5 модулей реестра (версия, статус, ограничения, `code_sha256`) |
| GET | `/v1/scenarios` | да | индекс 3 сценариев (`id`, `module_id`, `horizon_months`, `status`, `digest`) |
| GET | `/v1/scenarios/{scenario_id}` | да | один сценарий целиком (`data.result`) |
| GET | `/v1/forecast?target=&horizon=` | да | forecast-конверт таргета или проекция продуктового фида |

`scenario_id` — `oil.brent/hormuz_closed_6m`; форма `oil.brent__hormuz_closed_6m`
(из статического контракта) тоже принимается.

Таргеты (`/v1/forecast`): `oil_brent` (сценарий Ормуза, горизонт 6 мес., статус
`RESEARCH_ONLY`) и `cpi_yoy` (T1 CPI г/г h=1 из продуктового фида, статус `CANDIDATE`).
Алиасы нефти: `oil.brent`, `oil_brent`, `brent`; CPI: `T1_CPI_YOY`, `t1_cpi_yoy`, `cpi.yoy`, `cpi`.
`horizon` необязателен, но если указан — должен совпадать с опубликованным горизонтом таргета,
иначе `400 UNSUPPORTED_HORIZON` со списком допустимых; варианты сценариев (3 мес., без SPR) —
через `/v1/scenarios`.

Ответы — конверт:

```json
{
 "schema": "amfe.forecast_api.live.v1",
 "kind": "forecast",
 "api_version": "v1",
 "gateway_version": "1.0.0",
 "status": "RESEARCH_ONLY",
 "admission": false,
 "disclaimer": "Research-only scenario arithmetic served by a read-only gateway...",
 "policy": {"read_only": true, "research_only": true, "no_admission": true,
            "no_point_forecast": true, "no_raw_third_party_data": true,
            "deterministic": true, "api_key_required": true},
 "query": {"target": "oil_brent", "horizon": 6},
 "data": { "...ровно документ build/api/forecasts/oil_brent.json..." },
 "source": {"api_index": {"schema": "amfe.forecast_api.v2", "generated_at": "...",
                          "sha256": "...", "counts": {...}}},
 "links": {"self": "/v1/forecast?target=oil_brent&horizon=6", "scenarios": "/v1/scenarios",
           "modules": "/v1/modules", "status": "/v1/status", "openapi": "/openapi.json"}
}
```

`status` — худший статус из отданных сущностей (`RESEARCH_ONLY` < `CANDIDATE`); `admission`
всегда `false`; `disclaimer` и `policy` есть в каждом 200-ответе. Внутри `data` сохраняется
собственная версия контракта (`api_version: v2`) — шлюз не переписывает документы, а оборачивает.

Ошибки — тот же конверт с `kind: "error"`, `status: "ERROR"`, `admission: false`, `code`, `error`,
`details` (без внутренних путей и без трассировок):

| Код HTTP | `code` | Когда |
|---|---|---|
| 400 | `MISSING_PARAMETER`, `INVALID_HORIZON`, `UNSUPPORTED_HORIZON`, `UNKNOWN_PARAMETER`, `DUPLICATE_PARAMETER`, `METHOD_NOT_ALLOWED`, `INVALID_PATH`, `REQUEST_TOO_LARGE`, `TOO_MANY_PARAMETERS` | ошибка запроса; для `METHOD_NOT_ALLOWED` — заголовок `Allow: GET, OPTIONS` |
| 401 | `MISSING_API_KEY`, `INVALID_API_KEY`, `INVALID_AUTH_SCHEME` | нет/неверный ключ; `WWW-Authenticate: ApiKey realm="amfe"` |
| 404 | `NOT_FOUND`, `UNKNOWN_TARGET`, `UNKNOWN_SCENARIO` | нет пути/таргета/сценария; в `details` — доступные значения |
| 429 | `RATE_LIMITED`, `QUOTA_EXCEEDED` | минутная/суточная квота; `Retry-After` + `X-RateLimit-*` |
| 503 | `AUTH_NOT_CONFIGURED`, `FEED_UNAVAILABLE`, `FEED_INVALID`, `POLICY_VIOLATION`, `UNSUPPORTED_STATUS`, `PRODUCT_FEED_UNAVAILABLE`, `INTERNAL_ERROR` | fail-closed |

Каждый ответ несёт `X-Request-Id`, `X-API-Version`, `X-Gateway-Version`, `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff` и CORS-заголовки (`Access-Control-Allow-Origin`,
`-Methods: GET, OPTIONS`, `-Headers: X-API-Key, Authorization, Content-Type`,
`-Expose-Headers: X-Request-Id, Retry-After, X-RateLimit-Remaining`, `-Max-Age`). `OPTIONS`
(preflight) отвечает `200` без ключа. `HEAD` поддерживается (без тела).

## 4. Примеры (curl)

```powershell
$K = "demo_free_key_01"
# статус (публичный)
curl.exe -s http://127.0.0.1:8080/v1/status
# паспорта модулей (нужен ключ)
curl.exe -s -H "X-API-Key: $K" http://127.0.0.1:8080/v1/modules
# список сценариев
curl.exe -s -H "X-API-Key: $K" http://127.0.0.1:8080/v1/scenarios
# один сценарий
curl.exe -s -H "X-API-Key: $K" "http://127.0.0.1:8080/v1/scenarios/oil.brent/hormuz_closed_6m"
# forecast-конверт нефти (Bearer тоже работает)
curl.exe -s -H "Authorization: Bearer $K" "http://127.0.0.1:8080/v1/forecast?target=oil_brent&horizon=6"
# продуктовый таргет (точечная оценка скрыта, отдан интервал 5–95%)
curl.exe -s -H "X-API-Key: $K" "http://127.0.0.1:8080/v1/forecast?target=cpi_yoy&horizon=1"
# спецификация
curl.exe -s http://127.0.0.1:8080/openapi.json
```

PowerShell-эквивалент: `Invoke-RestMethod -Uri http://127.0.0.1:8080/v1/modules -Headers @{"X-API-Key"=$K}`.

Ожидаемые ошибки:

```text
без ключа           -> 401 {"code":"MISSING_API_KEY", ...}
чужой ключ          -> 401 {"code":"INVALID_API_KEY", ...}
target=nope         -> 404 {"code":"UNKNOWN_TARGET","details":{"available_targets":[...]}}
horizon=3 для oil   -> 400 {"code":"UNSUPPORTED_HORIZON","details":{"allowed_horizons":[6]}}
11-й запрос free/мин -> 429 {"code":"RATE_LIMITED", ...} + Retry-After
без AMFE_API_KEYS   -> 503 {"code":"AUTH_NOT_CONFIGURED", ...}
нет build/api       -> 503 {"code":"FEED_UNAVAILABLE", ...}  (/openapi.json остаётся 200)
```

## 5. Политика данных: что не отдаётся

1. **Никаких сырых сторонних наблюдений.** Перед выдачей каждый документ проходит
   `_scrub`: ключи вида `raw`, `raw_values`, `raw_data`, `observations`, `observed_values`,
   `source_rows`, `source_series`, `series`, `third_party_data`, `data_points` (и любые `*_raw`,
   `raw_*`) считаются нарушением → `503 POLICY_VIOLATION`. Это же правило действует на этапе
   загрузки: фид с такими полями не обслуживается вовсе.
2. **Никаких локальных путей и внешних URL.** Абсолютные пути (`G:/…`, `C:\…`, `/home/…`,
   `/mnt/…`), `http://`, `https://`, `file://` в значениях → `503 POLICY_VIOLATION`. Путь к
   реестру модулей публикуется только именем файла (`modules.jsonl`), не путём воркспейса.
3. **Никакого допуска.** `policy.no_admission = true` обязателен в индексе контракта; поле
   `admission` со значением `true` в любом документе → `503`. Статусы вне
   `{RESEARCH_ONLY, CANDIDATE}` (например, `ACTIVE`) → `503 UNSUPPORTED_STATUS`: шлюз не
   «понижает» статус молча и не притворяется, что модель допущена.
4. **Никакой точечной оценки по умолчанию.** Продуктовый фид содержит `point`; шлюз его скрывает
   (`point_withheld: true`, `no_point_forecast: true`) и отдаёт интервал
   `quantile_low/high = 5/95` из опубликованной сетки квантилей. Включение — только явным
   флагом `--allow-point`/`AMFE_API_ALLOW_POINT=1` (тогда `policy.no_point_forecast` становится
   `false`, что видно клиенту).
5. **Числа не выдумываются.** `data` — это ровно документ из `build/api` (тесты сверяют
   `price_path`, `summary`, `module_ref.code_sha256` с файлами контракта) либо проекция
   продуктового фида; метрики C4/C6 и ограничения передаются как есть.

## 6. Ограничения (честный список)

1. **Квоты в памяти процесса.** Перезапуск сбрасывает счётчики; при нескольких воркерах/репликах
   у каждого свой счётчик. Общий лимит — задача reverse proxy/Redis, не этого MVP.
2. **Нет биллинга и выставления счетов.** Тарифы — только лимиты частоты; оплата, счета,
   self-service выдача ключей и панель администратора не сделаны (ключи выдаются вручную через env).
3. **Нет TLS и mTLS.** Шлюз слушает loopback; TLS, HSTS и WAF — на reverse proxy. Наружу без
   прокси не выставлять.
4. **Нет разграничения по таргетам.** Любой валидный ключ видит все опубликованные таргеты;
   по-тарифного/по-клиентского entitlement нет.
5. **Статический снимок.** Данные читаются при старте; обновление — только перезапуск (или
   пересборка контракта + рестарт). Real-time стрима/вебхуков нет.
6. **Нет реконструкции/пересчёта.** Шлюз не запускает `OilModule` и не считает сценарии: если
   документа нет в `build/api`, ответ — 503/404, а не «посчитаем на лету».
7. **Один процесс, стандартная библиотека.** `ThreadingHTTPServer` без пула соединений, таймаутов
   чтения тела и HTTP/2; для нагрузки — прокси с `limit_req`, keep-alive и таймаутами.
8. **CORS по умолчанию `*`.** Для браузерных клиентов задайте `--cors-origin https://client...`;
   ключ при этом всё равно нельзя класть в query-строку.
9. **Продуктовый фид — CANDIDATE T1 CPI h=1.** Других таргетов продуктового фида шлюз не
   изобретает; отсутствие файла — `503 PRODUCT_FEED_UNAVAILABLE` для `cpi_yoy` и `DEGRADED` в
   `/v1/status` (рыночные таргеты продолжают работать).
10. **`/v1/status` публичный.** Он не содержит чисел прогноза, но раскрывает состав платформы и
    лимиты; если это нежелательно — закрыть на прокси по IP/хосту.
11. **Версионирование только `v1`.** Контракт-обёртка версионируется (`api_version: v1`,
    `schema: …live.v1`), upstream — `amfe.forecast_api.v2`; параллельных версий шлюза нет.
12. **SLA ниже — заготовка, не обязательство.** Research-beta: см. раздел 8.

## 7. Деплой

### 7.1 Локально (ноутбук, демо)

```powershell
$env:AMFE_API_KEYS = "demo_free_key_01:free"
python tools/forecast_api_server.py --host 127.0.0.1 --port 8080
```

Проверка: `curl.exe -s http://127.0.0.1:8080/v1/status` → `200`; `--check` → `"status": "OK"`.

### 7.2 За reverse proxy (рекомендуется)

Прокси берёт на себя TLS, лимиты по IP, логи без ключей и health-check. Пример nginx:

```nginx
upstream amfe_gateway { server 127.0.0.1:8080; keepalive 16; }

limit_req_zone $binary_remote_addr zone=amfe_ip:10m rate=5r/s;

server {
    listen 443 ssl http2;
    server_name api.example.org;
    ssl_certificate     /etc/ssl/api.example.org.pem;
    ssl_certificate_key /etc/ssl/api.example.org.key;

    # ключи только в заголовках: query-строку с api_key отбрасываем
    if ($args ~* "api_key=") { return 400; }

    location / {
        limit_req zone=amfe_ip burst=20 nodelay;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 15s;
        proxy_pass http://amfe_gateway;
    }

    location = /v1/status {
        access_log off;          # health-check без шума в логах
        proxy_pass http://amfe_gateway;
    }
}
```

Обязательные правила:

- `AMFE_API_KEYS` — только через env/секрет-стор (не в git, не в образе, не в командной строке);
- access-логи прокси без заголовков `X-API-Key`/`Authorization` (или с маскированием);
- health-check — `GET /v1/status` (200) или `GET /openapi.json` (200), но не `/v1/modules`
  (он требует ключ и тратит квоту);
- порт 8080 слушает только loopback (`--host 127.0.0.1`), наружу — исключительно прокси;
- ротация ключей — правка env + перезапуск (счётчики при этом сбрасываются, см. ограничение 1).

### 7.3 Как служба

Windows: NSSM/WinSW с `AMFE_API_KEYS` в переменных службы. Linux: unit-файл с
`EnvironmentFile=/etc/amfe/gateway.env`, `Restart=on-failure`, `User=amfe`. В обоих случаях —
`--check` как pre-start проверка (exit 2 не даёт поднять службу с пустыми ключами).

## 8. SLA-заготовка (research-beta, не обязательство)

| Параметр | Значение-заготовка | Комментарий |
|---|---|---|
| Доступность | 99,0% в месяц (beta) | окно обслуживания — перезапуск после пересборки контракта |
| RTO | 15 мин | поднять процесс + прокси из артефактов `build/api` |
| RPO | 0 (снимок) | данные не пишутся; потеря — только счётчики квот |
| Latency p95 | ≤ 150 мс на `/v1/forecast` (локально, без прокси) | тела ≤ ~16 КБ, без внешних вызовов |
| Ошибки 5xx | ≤ 0,5% запросов | 503 — штатный fail-closed при отсутствии ключей/фида |
| Поддержка | рабочие дни, 1 бизнес-день на вопрос по ключам/квотам | контакт — владелец подписки |
| Мониторинг | `/v1/status` каждые 60 с, алерт на 2 подряд неуспеха | плюс лог-метрика `429` (упёрлись в квоту) |

Что SLA **не** покрывает: точность прогноза (модель research-only, `admission=false`), свежесть
данных сверх даты пересборки контракта, доступность сторонних источников (их в шлюзе нет),
инвестиционные решения. Формулировки «прогноз точен», «допущено», «гарантирует доходность»
недопустимы в любых материалах подписки.

## 9. Тесты

```powershell
python tests/test_forecast_api_server.py     # 38 тестов, ~2,5 с, без сети
```

Покрытие (38):

- **статус (3):** публичность, схема/версии/`admission=false`, фиды+SHA-256, таргеты, лимиты,
  агрегированный статус `CANDIDATE` (худший из опубликованных);
- **аутентификация (3):** 401 для отсутствующего/неверного ключа и неверной схемы, `WWW-Authenticate`,
  Bearer, публичные эндпоинты без ключа, ключи не хранятся/не эхоируются;
- **модули и сценарии (2):** 5 паспортов `RESEARCH_ONLY` с `code_sha256` и ограничениями,
  сверка с реестром; список 3 сценариев и детальный документ = файл контракта, форма `__`;
- **forecast (5):** конверт нефти = `build/api/forecasts/oil_brent.json`, алиасы, валидация
  `horizon`/параметров (400/404 + `details`), продуктовый таргет скрывает `point` по умолчанию и
  отдаёт его при `allow_point`, `CANDIDATE`;
- **ошибки/CORS (3):** таблица кодов, `Allow` для не-GET, preflight, ограниченный CORS + `Vary`;
- **OpenAPI (1):** 3.0.3, все пути/схема безопасности/коды 200–503, `security: []` у `/v1/status`;
- **детерминизм (2):** побайтовая стабильность 200-ответов, `generated_at` не дрейфует;
- **no-raw (2):** нет `G:/`, `C:\`, `http(s)://`, `ACTIVE`/`LIMITED`, raw-ключей; disclaimer в
  каждом документе;
- **квоты (3):** минутный лимит + заголовки + `Retry-After`, суточная квота, независимость ключей;
- **fail-closed (10):** нет `index.json` → 503 (а `/openapi.json` — 200), нет файла сценария → 503,
  нет ключей → 503 (не открытый доступ), нет продуктового фида → `DEGRADED` + 503 только для
  `cpi_yoy`, отказ при `no_admission=false`, raw-ключе, абсолютном пути, внешнем URL, статусе
  `ACTIVE`, неверной конфигурации ключей (exit 2);
- **HTTP (1):** реальный `http.server` на свободном порту: 200/401/404/400/OPTIONS/HEAD, CORS,
  `X-Request-Id`, `X-Plan`;
- **CLI (3):** `--check` (OK / BLOCKED / DEGRADED, exit 0/2), `--version`, `--help`, exit 2 при
  плохом `AMFE_API_KEYS`.

## 10. Воспроизведение

```powershell
$env:AMFE_API_KEYS = "demo_free_key_01:free,demo_pro_key_01:pro"
python tools/forecast_api_server.py --check
python tools/forecast_api_server.py --port 8080
# в другом окне:
curl.exe -s -H "X-API-Key: demo_free_key_01" "http://127.0.0.1:8080/v1/forecast?target=oil_brent&horizon=6"
python tests/test_forecast_api_server.py
```

## 11. Ссылки

- [docs/156](156_Forecast_API_и_рыночный_пакет.md) — контракт `amfe.forecast_api.v2` и клиентский пакет;
- [docs/161](161_Реестр_API_паспорта.md) — паспорта модулей в API (`module_ref`, `/v1/modules`);
- [docs/157](157_Реестр_модулей.md) — реестр модулей и переходы статусов;
- [docs/174](174_50_кейсов_использования.md) — UC-45 и приоритеты (P0/26);
- [tools/forecast_api.py](../tools/forecast_api.py) — статический контракт (источник документов);
- [build/api/README.md](../build/api/README.md) — состав опубликованных файлов;
- `tests/test_forecast_api.py` — тесты статического контракта (38 тестов шлюза — этот документ).
