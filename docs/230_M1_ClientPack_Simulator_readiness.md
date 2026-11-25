# 230. M1 readiness: Client Pack v2 и Simulator

Дата: 2026-11-23 (обновлено 2026-11-25: PDF-экспорт и демо-сценарий закрыты). Аудит локальных
артефактов; **не подтверждает production-деплой**. Все статусы `RESEARCH_ONLY`, `admission=false`.

## 1. Client Pack v2 — PARTIAL / locally buildable

Команда проверки (output записан во временный каталог, не в репозиторий):

```powershell
python tools/forecast_world_bridge.py --feed build/product/forecast_latest.json --dataset figaro --partial `
  --out build/forecast_world_bridge --generated-at 2026-11-23T12:00:00Z
python tools/client_pack_v2.py --out <TEMP> --client "M1 Demo" --scenario hormuz_closed_6m `
  --generated-at 2026-11-23T12:00:00Z --bridge build/forecast_world_bridge/forecast_world.json `
  --include-blocks --json
```

Результат: `ok=true`, schema `amfe.client_pack.v2`, status `RESEARCH_ONLY`; 9 API modules,
2 market modules, 2 blocks (15/17), 6 block scenarios, 15 bridge effects, 2 forecast targets,
9 source records. Outputs: executive summary DOCX, numbers XLSX, dashboard HTML, machine-readable
JSON, methodology DOCX, licence/disclaimer DOCX, reproducibility JSON, README — и, с 2026-11-25,
три PDF-версии DOCX.

| Приёмка M1 | Статус | Комментарий |
|---|---|---|
| 9 модулей | PASS | 9 статусов `RESEARCH_ONLY` |
| Bridge + blocks 15/17 | PASS* | 2 blocks / 6 scenarios / 15 effects; hashes included |
| PDF client pack | **PASS (2026-11-25)** | 3 PDF из DOCX (Executive / Methodology / Licence); `non_deterministic_artifacts` с SHA-256 и числом страниц; байт-детерминизм 7 файлов сохранён; `--no-pdf` отключает |
| Deterministic metadata | PASS | reproducibility JSON; hashes; PDF явно вынесены из `deterministic_artifacts` |
| No admission claims | PASS | `admission=false`, candidate remains CANDIDATE |
| Full forecast bridge | **PARTIAL** | bridge reports `PARTIAL_INPUTS`; T2_GDP_GROWTH/T3_RECESSION absent; growth scenario blocked |
| Production deployment | **NOT TESTED** | local build only, no staging/prod URL |


`*` Bridge is a partial research artifact, not an admitted world forecast. It includes no raw external data.

## 2. Simulator — configuration PASS / deployment PARTIAL

`python tools/simulator_ui.py --check` → schema `amfe.simulator.ui.v1`, version 1.2.0,
`RESEARCH_ONLY`, `admission=false`. Checks passed: aggregate-only, deterministic, oil/gas/geopolitics/
institutions/technology modules. Auth is optional for local demo and required for pilot using
`--require-auth`; no production keys or deployment tested.

| Приёмка M1 | Статус | Комментарий |
|---|---|---|
| Scenario config | PASS | 6 scenario families available; controlled illustrative inputs |
| Aggregate-only / deterministic | PASS | tool self-check |
| API key / quota controls | PASS for config | pilot mode supported; not a security certification |
| 30-minute demo runbook | **PASS (локально)** | 6 шагов по одному на сценарий, валидатор и прогон в процессе — [docs/231](231_Simulator_демо_сценарий.md); внешнего тайминга с клиентом ещё нет |
| TLS / gateway / staging | **NOT TESTED** | no deployment claim |

## 3. Next 3 actions

1. ~~Add optional PDF export of the Client Pack~~ — **сделано 2026-11-25**: PDF рендерится из DOCX через headless
   LibreOffice, учитывается как недетерминированный артефакт (свой SHA-256 и число страниц), байт-детерминизм
   манифеста сохранён; `--no-pdf` отключает.
2. ~~Make a 30-minute scripted Simulator demo~~ — **сделано 2026-11-25**: 6 шагов, `--check/--run/--runbook`,
   прогон в процессе без сервера, 22 теста ([docs/231](231_Simulator_демо_сценарий.md)).
3. ~~Decide whether M1 accepts a `PARTIAL_INPUTS` bridge~~ — **решено 2026-11-25: GO с оговоркой**,
   M1 принимает partial bridge (2 из 3 сценариев, рост `BLOCKED_INPUT`, без синтетики T2/T3) —
   [docs/234](234_Мост_PARTIAL_INPUTS_решение.md); критерии пересмотра там же.

## 4. Commercial track in parallel

- 25 fixed-price institutional offers are separate from forecast-admission status; all are hypotheses.
- Commercial Strategy agent and public offer page are being added in parallel; no contacts or messages are sent.
- Legal, access, and procurement checks route to Human Counsel/Lead; no gated source is accessed without authorization.
