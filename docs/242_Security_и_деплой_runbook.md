# 242. Security и деплой runbook для пилота (без деплоя)

Версия 1.0 · 2026-11-26 · Роль: независимый security/deploy reviewer. **Это не деплой, не security-сертификация и не разрешение на публикацию.** Решение о пилоте принимает человек; документ фиксирует, что уже есть, чего нет и что обязательно сделать до внешнего доступа. Код не изменялся; серверы не запускались; ничего не деплоилось и не коммитилось.

Статусы продукта: `RESEARCH_ONLY`, `admission=false`, `causal_claim=false` ([docs/192 §0](192_Simulator_UI.md), [docs/230](230_M1_ClientPack_Simulator_readiness.md), [docs/235](235_Simulator_live_smoke.md)). Никакой пилот не должен превращать эти статусы в «допущенный прогноз».

## 1. Что уже есть (факты, не обещания)

### 1.1. Simulator UI (демо-сервер)

Файл: [tools/simulator_ui.py](../tools/simulator_ui.py), документация [docs/192](192_Simulator_UI.md).

| Факт | Где |
|---|---|
| Один процесс на stdlib `http.server` / `ThreadingHTTPServer`; без Flask/FastAPI/CDN | `tools/simulator_ui.py:74`, `:2184–2187` |
| Bind по умолчанию **127.0.0.1:8090**; `--host/--port` есть | `tools/simulator_ui.py:93–94`, `:2856–2858`; docs/192 §1/§6 |
| 6 сценариев; `health` и `/` публичны | docs/192 §2/§4; `tools/simulator_ui.py:99` |
| CSP: `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` | `tools/simulator_ui.py:2074–2077`; docs/192 §6 |
| `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` | `tools/simulator_ui.py:2171–2172`; docs/192 §6 |
| Auth **опционален**, по умолчанию выключен; `--require-auth`; публичные `/`, `/index.html`, `/api/health`, `/favicon.ico` | `tools/simulator_ui.py:41–44`, `:99`, `:2863–2866`; docs/192 §7 |
| Ключи: `X-API-Key`/`Api-Key` или `Authorization: Bearer`; в query не принимаются | `tools/simulator_ui.py:341–351`; docs/192 §7.1/§7.3 |
| В памяти только SHA-256 дайджесты; plaintext не логируется/не эхоируется | `tools/simulator_ui.py:247–250`, `:2044`; docs/192 §7.4 |
| Тарифы free/pro/internal; free не может `POST /api/run` (403); квота на ключ, **в памяти**, окно 1 час | docs/192 §7.2; `tools/simulator_ui.py:260–318` |
| 401/403/429; `WWW-Authenticate`, `Retry-After`, `X-RateLimit-*`, `X-Plan` | docs/192 §7.3; `tools/simulator_ui.py:171–175`, `:315–318` |
| Fail-closed конфигурация: `--require-auth` без ключей, ключи без auth, короткий ключ/тариф/дубликат/квота < 1 → exit 2 | `tools/simulator_ui.py:363–376`; docs/192 §7.4 |
| Лог аутентификации: `client=<8 hex>` (префикс SHA-256), plan, remaining; ключа нет | `tools/simulator_ui.py:2044`, docs/192 §7.4 |

**Чего нет:** TLS/HSTS, WAF, reverse proxy, self-service ключей, ролей пользователей, биллинга, постоянного аудита/хранилища квот; квоты сбрасываются при рестарте и не агрегируются между воркерами; один процесс не рассчитан на нагрузку. Это прямо признано в docs/192 §7.5 (строки 265–272) и §8.7.

### 1.2. Forecast Gateway (живой шлюз)

Файл: [tools/forecast_api_server.py](../tools/forecast_api_server.py), docs [183](183_Forecast_API_шлюз.md), [226](226_API_SLA_метрики.md), [233](233_Prometheus_метрики_и_алерты.md).

| Факт | Где |
|---|---|
| stdlib `http.server`/`ThreadingHTTPServer`; статический контракт, без пересчёта модели | `tools/forecast_api_server.py:60`, `:1725–1727`; docs/183 §0 |
| Bind по умолчанию **127.0.0.1:8080**; снимок фидов читается при старте | `tools/forecast_api_server.py:68–69`, `:1762–1776`; docs/183 §1 |
| Ключи из `AMFE_API_KEYS`; `X-API-Key`/`Authorization: Bearer`; в query не принимаются | `tools/forecast_api_server.py:24–29`, `:210–234`, `:1044–1061` |
| Только SHA-256 дайджесты в памяти; `hmac.compare_digest`; plaintext не логируется | `tools/forecast_api_server.py:210–234`, `:248`; docs/183 §2 |
| Квоты per-key, minute/day, in-memory; 429 + `Retry-After`/`X-RateLimit-*` | `tools/forecast_api_server.py:267–318`, `:992–1011`; docs/183 §2 |
| Публичные endpoints без ключа: `/openapi.json`, `/v1/status`, `/v1/health`, `/v1/metrics`, `/v1/metrics/prometheus`, `/v1/sla` | `tools/forecast_api_server.py:981–988`, `:1031–1033`; docs/226 §1, docs/233 §1 |
| `/v1/health` — liveness/readiness, `/v1/metrics` — JSON, `/v1/metrics/prometheus` — text exposition 0.0.4 | docs/226, docs/233 §1/§3 |
| `/v1/sla`: declared targets, **`contractual=false`** (не SLA-договор) | `tools/forecast_api_server.py:95–100`, `:1208–1217`; docs/226 §2 |
| Заголовки: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Request-Id`, версии; CORS настраивается (по умолчанию `*`) | `tools/forecast_api_server.py:890–905`; docs/183 §1 |
| Fail-closed: неправильные ключи/фиды → отказ; ошибки 503; admission=true в фиде не обслуживается | docs/183 §0/§4; `tools/forecast_api_server.py:327–368` |

**Чего нет:** TLS/SSL в коде, HSTS, WAF, reverse proxy, secret-store клиент, ротация ключей, биллинг, Alertmanager/on-call, внешней БД. CORS `*` — demo default; для пилота нужен точный origin. `/v1/metrics/prometheus` и health/metrics публичны по дизайну demo; наружу их надо закрыть/ограничить на прокси (docs/233 §5, строки 122–124).

### 1.3. Мониторинг/алерты

`deploy/prometheus/amfe_alerts.yml` — группа `amfe-forecast-gateway`, **4 правила**:

| Alert | expr | for | severity |
|---|---|---|---:|
| `AmfeGatewayDown` | `amfe_gateway_up == 0` | 2m | critical |
| `AmfeHighErrorRate` | `amfe_error_rate > 0.05` | 5m | warning |
| `AmfeHighLatencyP95` | `amfe_request_duration_p95_ms > 500` | 5m | warning |
| `AmfeNoTraffic` | `increase(amfe_requests_total[10m]) == 0` | 10m | warning |

Метрики in-process, обнуляются при рестарте; `p50/p95` — lifetime, не оконные гистограммы; правила — **файл**, Prometheus/Grafana/Alertmanager/on-call в репозитории нет (docs/233 §3/§5). Пороговые 5%/500 ms — демо, не SLA.

### 1.4. Что подтверждено локально и что нет

- [docs/235](235_Simulator_live_smoke.md): **smoke PASS только локально** (`127.0.0.1:8095`, 6/6 сценариев, CSP/nosniff/no-store, детерминизм). TLS, reverse proxy, WAF, внешний деплой **не проверялись** (docs/235 §3).
- [docs/230](230_M1_ClientPack_Simulator_readiness.md): Simulator config PASS; auth/quota config PASS; **production deployment NOT TESTED**, TLS/gateway/staging **NOT TESTED** (строки 32, 37–50).
- [docs/226](226_API_SLA_метрики.md) §5: SLA не договорной, метрики in-memory, нет TLS/прокси/алертов/on-call.
- [docs/227](227_25_коммерческих_офферов.md), [docs/228](228_Commercial_Strategy.md), [agents/commercial](../agents/commercial/README.md): 25 офферов — `DRAFT/HYPOTHESIS`, `not_client=true`, `sent=false`; ROI/точность/допуск не обещаются; NDA/DPA/legal — через Human Counsel; реальных клиентов/подписанных документов нет.

## 2. Три главных блокера до внешнего пилота

1. **Нет TLS/reverse proxy/WAF.** Оба сервера — plain HTTP stdlib, bind localhost; TLS/HSTS/WAF/прокси отсутствуют (docs/192 §7.5; docs/235 §3; docs/230). Публиковать `--host 0.0.0.0` нельзя; нужен TLS-терминирующий reverse proxy, закрывающий 8080/8090, с WAF/rate-limit.
2. **Нет secret-store и жизненного цикла ключей.** Ключи передаются через env (`AMFE_API_KEYS`/`AMFE_SIM_KEYS`) или `--api-keys` (последнее видно в истории процессов); хранение — только SHA-256 в памяти, без Vault/KMS, срока жизни, ротации и отзыва. Нужны secret manager, инъекция в процесс (не CLI), версии/ротация, аудит выдачи.
3. **Нет DPA/NDA/политики данных и операционного контура.** В репозитории нет подписанных DPA/NDA/terms, политики хранения/удаления, аудит-лога с retention, on-call/Alertmanager, backup/RPO/RTO. Commercial-офферы — гипотезы, не клиенты; legal/DPA — только маршрут к Human Counsel (docs/227 §4, docs/228 §4). Без этого внешний пилот с клиентскими данными недопустим.

**Готовность: НЕ ГОТОВ к внешнему пилоту.** Локальный конфиг/смоук — да; production security/legal/ops — нет. Формально: 0/3 внешних блокера закрыто; чек-лист §5 — 15 пунктов, ни один не подтверждён в этой среде. После закрытия 3 блокеров и 15/15 чек-листа можно говорить «готов к ограниченному пилоту», но не «сертифицирован».

## 3. Обязательно до внешнего пилота

| Область | Требование | Почему |
|---|---|---|
| TLS | TLS 1.2+/1.3 на reverse proxy, сертификат, HTTP→HTTPS redirect, HSTS; никакого прямого доступа к 8080/8090 | код TLS не реализует (docs/192 §7.5; docs/233 §5) |
| Proxy | nginx/Caddy/HAProxy, только 443 наружу; upstream 127.0.0.1:8080/8090; `server_tokens off`; лимиты тела/заголовков | stdlib-серверы не production |
| Secrets | ключи из Vault/KMS/secret manager, инъекция в env процесса; **не** `--api-keys`, не файл в git, не история shell; ротация/отзыв | `--api-keys` виден в process list; env тоже требует защиты |
| WAF/rate-limit | WAF + per-IP/per-key rate limit на прокси; приложенческие квоты — второй слой | app-квоты in-memory, не защищают от DDoS |
| CORS | для gateway задать точный `--cors-origin`/`AMFE_API_CORS_ORIGIN`, не `*`; для simulator CORS не нужен | default `*` — demo; docs/183 §1 |
| Headers | HSTS, CSP (simulator), `X-Content-Type-Options`, `Cache-Control`, `Referrer-Policy`, скрыть `Server`/`X-Powered-By` | CSP/nosniff уже есть в simulator; TLS/HSTS нет |
| Auth | обязательный `--require-auth` для simulator, обязательные ключи для gateway; публичные — только `/`, `/api/health` (sim) и health/metrics/sla (gateway) по политике | docs/192 §7; docs/233 §1 |
| Audit | access/audit log на прокси и приложении: timestamp, request-id, path, status, client hash/plan, без ключей/тел/персональных данных; retention и защита от подмены | сейчас только stderr-логи, нет persistent audit |
| Data | DPA/NDA/terms подписаны; data classification; никаких raw/third-party/PII; только разрешённые агрегаты; клиентские данные — отдельное решение | docs/227/228; продукт research-only |
| Retention/deletion | сроки хранения логов/артефактов/метрик; процедура удаления по запросу; подтверждение удаления; не хранить секреты | отсутствует |
| Key rotation | процедура ротации, overlap-окно, отзыв, проверка старый 401/новый 200; ротация при инциденте | нет |
| Backup/RPO/RTO | backup конфигов/прокси/сертификатов/секретов/манифестов; RPO/RTO утверждены; restore-тест; артефакты воспроизводимы | нет |
| Incident | контакт, on-call, kill-switch, порядок эскалации, уведомление клиента/юриста, postmortem | нет on-call/Alertmanager |
| Monitoring | Prometheus scrape + 4 правила загружены; Alertmanager/канал; тест алерта; health-check прокси | файл есть, развёртывания нет |
| Commercial | офферы остаются HYPOTHESIS; никаких ROI/accuracy/admitted claims; все внешние коммуникации — только после human approval | docs/227/228 |

## 4. Топология пилота (предлагаемая, не развёрнутая)

```
Internet
  │ 443/TLS (HSTS)
  ▼
TLS reverse proxy / WAF  (nginx/Caddy/HAProxy; сертификат; rate-limit; access log)
  ├── api.<pilot-domain>  → 127.0.0.1:8080  Forecast Gateway
  │      /v1/*             (health/metrics/sla/status/modules/scenarios/forecast)
  └── demo.<pilot-domain> → 127.0.0.1:8090  Simulator UI
         /, /api/health, /api/scenarios, /api/run
```

- Наружу — **только 443**; 8080/8090 — loopback, firewall deny; прямой доступ к ним запрещён.
- Отдельные hostname для API и demo: у simulator `/` — HTML-страница, у gateway `/` — контракт; смешивать на одном origin не надо.
- Заголовки клиента: `Authorization: Bearer <key>` или `X-API-Key`; TLS-прокси передаёт `X-Forwarded-For/Proto/Host` только доверенному upstream; ключи не логируются.
- `/v1/metrics` и `/v1/metrics/prometheus` — только monitoring network/private route; `/v1/health`, `/v1/status`, `/openapi.json` — по политике; simulator `/api/health` — public by design, `/` — public page.
- CORS: gateway `--cors-origin https://demo.<pilot-domain>` (если браузерный клиент); иначе без CORS. Simulator не требует CORS.
- Секреты: Vault/KMS → inject `AMFE_API_KEYS` (gateway) / `AMFE_SIM_KEYS` (simulator) в env процесса; никогда в CLI/репозиторий; права только сервисному пользователю.
- Health/metrics проверки — **только локально/из monitoring network**: `curl http://127.0.0.1:8080/v1/health`, `/v1/metrics/prometheus`, `curl http://127.0.0.1:8090/api/health`; снаружи — через прокси с ограничением.

## 5. Fail-closed чек-лист приёмки деплоя (15 пунктов)

Каждый пункт — проверяемое наблюдение/команда; без «на глаз». Пока все 15 не PASS — пилот не принимается.

| # | Проверка | Ожидаемый результат |
|---:|---|---|
| 1 | TLS-сертификат и цепочка на `api.` и `demo.` (`openssl s_client -connect host:443 -servername ...`) | валидный сертификат, TLS 1.2+, без ошибок; HTTP→HTTPS 301/308 |
| 2 | Сканирование портов с внешнего хоста (`nmap`/firewall review) | открыт только 443; 8080/8090 недоступны снаружи |
| 3 | Конфиг прокси (`nginx -t`/`caddy validate`) + host header | синтаксис OK; неизвестный Host → 421/403; proxy не раскрывает версию |
| 4 | Заголовки ответов `curl -I https://...` | HSTS, CSP (simulator), nosniff, no-store, Referrer-Policy; нет `Server`/`X-Powered-By` |
| 5 | Ключи из secret store: `ps`/history/`env` не содержат plaintext; процесс получает env | нет плейнтекста в истории/командной строке; только digest в памяти; секрет-версия зафиксирована |
| 6 | Auth matrix: без ключа/неверный/`free` POST/`pro` POST (gateway и simulator) | 401/401/403/200; `WWW-Authenticate`/plan headers |
| 7 | Rate limit/WAF burst (например, 100 запросов за минуту с одного IP/ключа) | 429/403, `Retry-After`; приложение не падает |
| 8 | CORS: preflight с разрешённого и запрещённого origin | разрешённый origin — OK; чужой/`*` — нет; gateway `--cors-origin` точный |
| 9 | Health/metrics: gateway `/v1/health`, `/v1/metrics`, `/v1/metrics/prometheus`, `/v1/sla`; simulator `/api/health` | health OK; metrics растут; prometheus content-type 0.0.4; `contractual=false` |
| 10 | Prometheus scrape + rules: `promtool check rules`, target UP, 4 alerts loaded | all rules valid; `amfe_gateway_up=1`; пробный alert срабатывает; Alertmanager/on-call подключён |
| 11 | Audit/access log: сделать запрос с ключом и телом; `grep` по логам | есть timestamp/request-id/status/path; **нет** ключа/Authorization/тела/PII; retention задан |
| 12 | DPA/NDA/terms/legal: проверить подписанные документы и data-flow | подписаны до передачи данных; юрисдикция/роли/контакт DPO/legal зафиксированы |
| 13 | Retention/deletion: тест удаления тестовой записи/лога; проверить сроки | запись удалена; подтверждение/аудит удаления; raw/PII отсутствуют |
| 14 | Key rotation: добавить новый ключ, отозвать старый, перезапустить | старый 401, новый 200; сервис без простоя или с согласованным окном; ротация записана |
| 15 | Backup/RTO/rollback: restore конфига/секрет-версии/сертификата в тест; kill-switch | restore успешен; RPO/RTO соблюдены; откат к предыдущей версии проверен; логи/артефакты сохранены |

**Не проверяется этим runbook:** нагрузочное тестирование, DDoS-устойчивость, пентест, юридическая квалификация DPA/NDA, сертификация (ISO/SOC и т.п.), качество прогноза/модели, биллинг. Эти пункты требуют отдельных специалистов/решений; нельзя считать их покрытыми чек-листом.

## 6. План отката

**Триггеры:** утечка/подозрение на утечку ключа или данных; TLS/сертификат/прокси-инцидент; аномальные 5xx/латентность; юридическое возражение/отзыв согласия; нарушение research-only/admission=false; ошибка в опубликованном контракте.

1. **Kill-switch на прокси:** deny all для demo/api (503/403), не удаляя логи; при необходимости остановить upstream-процессы gateway/simulator.
2. **Ключи:** отозвать все pilot-ключи, выпустить новые; при инциденте считать старые скомпрометированными.
3. **Сохранить артефакты:** access/audit logs, metrics snapshots, конфиг прокси, версии сертификатов/секретов, request-id, ответы health/status; не перезаписывать.
4. **Уведомить:** Lead/owner, legal/DPO (если данные), затронутого клиента — по утверждённому шаблону; не обещать причину/масштаб до проверки.
5. **Восстановить:** предыдущая версия прокси/конфига/секрет-версии; сервис только после 15/15 повторной приёмки и подтверждения безопасности.
6. **Postmortem:** таймлайн, причина, данные/ключи, corrective actions, обновление runbook; запись в журнал инцидентов. Не удалять логи до истечения retention/legal hold.

## 7. Явные ограничения и честный статус

- Это **runbook, а не деплой**: ни один сервер не поднят, ни один порт/сертификат/прокси/секрет не создан; команды в чек-листе — для будущего исполнения человеком/ops, не выполнены.
- Это **не security-сертификация** и не пентест; чек-лист не заменяет ISMS/ISO/SOC/legal review.
- `RESEARCH_ONLY`, `admission=false`, `causal_claim=false` сохраняются; пилот не даёт права называть модель допущенной, прогнозом, ROI или гарантией.
- TLS: **отсутствует** в коде; Secret store: **отсутствует** (env/CLI + in-memory SHA-256); DPA/NDA: **отсутствуют** (только маршрут к Human Counsel; офферы — HYPOTHESIS).
- M1 readiness: локальный config/smoke PASS, **production deployment NOT TESTED**; внешний пилот — **не готов** до 3 блокеров и 15/15 чек-листа.
- В этой среде не запускались серверы, не проверялись TLS/прокси/WAF/нагрузка/пентест/DPA; документ основан на чтении кода и docs/183/192/226/227/228/230/233/235.
