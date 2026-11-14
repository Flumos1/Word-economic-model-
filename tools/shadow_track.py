"""Shadow-track monitor for the admitted research candidate (v5 SHADOW_TEST).

Reads the append-only forward journal, resolves the rows whose facts are published and reports the
distance to the C6 gate (12 resolved forecasts, >=4 distinct target periods, cumulative relative loss
<= 1.0, interval coverage within tolerance) plus cadence/loss/coverage alerts.  Derived artefact: it
never writes the journal, never changes a protocol and never contains raw third-party observations.

Status semantics (docs/119):
  STARTING   - no resolved forecast yet, nothing to judge;
  ON_TRACK   - resolved rows exist, all alerts are lower than WARN;
  WARN       - an early-warning threshold is crossed (cumulative relative loss > 1.0, or a cadence
               gap is approaching, or coverage tolerance is broken with a small sample);
  ALERT      - a suspension threshold is crossed (cumulative relative loss > 1.10 after >= 6
               resolved, coverage outside tolerance after >= 6 resolved, or no new row for > 35 days);
  GATE_PASS  - the C6 gate is passed (the candidate is ready for LIMITED_DEPLOYMENT review).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import registry_resolve  # noqa: E402

SCHEMA = "AMFE-SHADOW-TRACK/1.0.0"
DEFAULT_CANDIDATE = "ENSEMBLE_REGIME_V4"
DEFAULT_TARGET = "T1_CPI_YOY"
DEFAULT_HORIZON = 1
MIN_FORECASTS = 12
MIN_DISTINCT_PERIODS = 4
CADENCE_ALERT_DAYS = 35
CADENCE_WARN_DAYS = 25
LOSS_ALERT = 1.10
LOSS_WARN = 1.00
MIN_RESOLVED_FOR_LOSS_ALERT = 6
RELEASE_WEEKS_PER_MONTH = 2.5  # CPI + payrolls, sometimes GDP/claims


class ShadowTrackError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_candidate_report(root: Path, candidate_id: str) -> dict | None:
    """Find the evaluation report of the candidate (by report admission/candidate fields)."""
    for name in ("regime_v4_candidate_evaluation_report.json", "regime_v3_candidate_evaluation_report.json",
                 "regime_v2_candidate_evaluation_report.json", "regime_candidate_evaluation_report.json"):
        path = root / "protocols" / name
        if not path.exists():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("candidate") == candidate_id:
            return {"file": f"protocols/{name}", "sha256": sha256_file(path), "report": report}
    return None


def build_status(root: Path, candidate_id: str, target: str, horizon: int,
                 today: date | None = None, generated_at: str | None = None) -> dict:
    journal = root / "registry" / "forecasts.jsonl"
    if not journal.exists():
        raise ShadowTrackError(f"journal not found: {journal}")
    report = load_candidate_report(root, candidate_id)
    if report is None:
        raise ShadowTrackError(f"no evaluation report registered for candidate {candidate_id}")
    admission = report["report"]["admission"]
    if admission.get("status") not in ("SHADOW_TEST", "LIMITED_DEPLOYMENT", "ACTIVE", "CANDIDATE"):
        raise ShadowTrackError(f"unexpected admission status: {admission.get('status')!r}")

    resolution = registry_resolve.resolve_rows(journal)
    resolved_candidate = [item for item in resolution["resolved"]
                          if item.get("candidate_id") == candidate_id
                          and item.get("target") == target
                          and int(item.get("horizon", 0)) == int(horizon)]
    if resolved_candidate:
        c6 = registry_resolve.c6_for_candidate(resolution["resolved"], candidate_id, target, int(horizon))
    else:
        c6 = {"n_resolved": 0, "min_forecasts": MIN_FORECASTS, "count_met": False,
              "per_loss": {}, "coverage": None, "coverage_met": True, "pass": False,
              "candidate_id": candidate_id, "origins": []}
    rows = registry_resolve.collect(journal)["rows"]
    cand_rows = sorted(
        (row for row in rows
         if row.get("candidate_id") == candidate_id and row.get("target") == target
         and int(row.get("horizon", 0)) == int(horizon)),
        key=lambda row: (row.get("origin"), row.get("vintage_week") or ""))
    distinct_periods = sorted({registry_resolve._target_period_label(target, row.get("origin"),
                                                                     int(horizon))
                               for row in rows
                               if row.get("candidate_id") == candidate_id
                               and row.get("target") == target
                               and int(row.get("horizon", 0)) == int(horizon)})
    today = today or date.today()
    registered = sorted(row["registered_at"] for row in cand_rows if row.get("registered_at"))
    last = registered[-1] if registered else None
    days_since = None
    if last:
        try:
            days_since = (datetime.fromisoformat(last.replace("Z", "+00:00")).date()
                          if len(last) > 10 else date.fromisoformat(last)) - today
            days_since = -days_since.days
        except ValueError:
            days_since = None

    alerts: list[dict] = []
    count = int(c6.get("n_resolved", 0))
    per_loss = c6.get("per_loss") or {}
    coverage_met = bool(c6.get("coverage_met", True))
    cumulative = {loss: float(entry.get("cumulative_relative_loss", 0.0))
                  for loss, entry in per_loss.items()}
    if count >= MIN_RESOLVED_FOR_LOSS_ALERT:
        worst = max(cumulative.values()) if cumulative else 0.0
        if worst > LOSS_ALERT:
            alerts.append({"code": "SUSPEND_LOSS", "severity": "ALERT",
                           "message": f"cumulative relative loss {worst:.4f} > {LOSS_ALERT:.2f} "
                                      f"after {count} resolved forecasts"})
        elif worst > LOSS_WARN:
            alerts.append({"code": "WARN_LOSS", "severity": "WARN",
                           "message": f"cumulative relative loss {worst:.4f} > 1.00 "
                                      f"after {count} resolved forecasts"})
    if count >= MIN_FORECASTS and not coverage_met:
        alerts.append({"code": "SUSPEND_COVERAGE", "severity": "ALERT",
                       "message": f"interval coverage outside tolerance after {count} resolved"})
    if days_since is not None and days_since > CADENCE_ALERT_DAYS:
        alerts.append({"code": "SUSPEND_CADENCE", "severity": "ALERT",
                       "message": f"no new candidate row for {days_since} days "
                                  f"(> {CADENCE_ALERT_DAYS})"})
    elif days_since is not None and days_since > CADENCE_WARN_DAYS:
        alerts.append({"code": "WARN_CADENCE", "severity": "WARN",
                       "message": f"no new candidate row for {days_since} days"})

    if c6.get("pass") and len(distinct_periods) >= MIN_DISTINCT_PERIODS:
        status = "GATE_PASS"
    elif any(alert["severity"] == "ALERT" for alert in alerts):
        status = "ALERT"
    elif any(alert["severity"] == "WARN" for alert in alerts):
        status = "WARN"
    elif count == 0:
        status = "STARTING"
    else:
        status = "ON_TRACK"

    remaining = max(0, MIN_FORECASTS - count)
    months = remaining / RELEASE_WEEKS_PER_MONTH if remaining else 0.0
    # a row resolves about one month after the origin month end: add the resolution lag explicitly
    estimated = (today + timedelta(days=round(30.0 * (months + 1.0)))).isoformat() if remaining else None
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": today.isoformat(),
        "candidate": {"id": candidate_id, "status": admission.get("status"),
                      "status_label": admission.get("status_label"),
                      "conditional_data_class": bool(admission.get("conditional_data_class")),
                      "report": {"file": report["file"], "sha256": report["sha256"]}},
        "pair": {"target": target, "horizon": int(horizon)},
        "journal": {"rows_total": len(rows), "rows_candidate": len(cand_rows),
                    "first_registered_at": registered[0] if registered else None,
                    "last_registered_at": last,
                    "last_vintage_week": cand_rows[-1].get("vintage_week") if cand_rows else None,
                    "days_since_last_row": days_since},
        "c6": {"n_resolved": count, "min_forecasts": MIN_FORECASTS,
               "count_met": count >= MIN_FORECASTS,
               "distinct_target_periods": len(distinct_periods),
               "min_distinct_target_periods": MIN_DISTINCT_PERIODS,
               "distinct_periods_met": len(distinct_periods) >= MIN_DISTINCT_PERIODS,
               "cumulative_relative_loss": cumulative,
               "coverage": c6.get("coverage"), "coverage_met": coverage_met,
               "pass": bool(c6.get("pass"))},
        "alerts": alerts,
        "status": status,
        "projection": {"release_weeks_per_month": RELEASE_WEEKS_PER_MONTH,
                       "rows_remaining": remaining,
                       "months_to_min_forecasts": round(months, 1),
                       "estimated_gate_date": estimated,
                       "assumption": "weekly rows only in weeks with a first data release; about one "
                                     "month of resolution lag after the target period"},
        "rules": {"suspend_loss": LOSS_ALERT, "warn_loss": LOSS_WARN,
                  "suspend_coverage": "outside the nominal tolerance after >= 12 resolved",
                  "suspend_cadence_days": CADENCE_ALERT_DAYS,
                  "reference": "docs/119, protocol v5 (C6)"},
    }


def render_markdown(document: dict) -> str:
    cand = document["candidate"]
    c6 = document["c6"]
    journal = document["journal"]
    lines = [
        f"# AMFE — теневой трек {cand['id']} ({document['date']})",
        "",
        f"> Статус кандидата: **{cand['status']}**"
        + (f" (`{cand['status_label']}`)" if cand.get("status_label") else "")
        + f"; `conditional_data_class = {str(cand['conditional_data_class']).lower()}`.",
        f"> Статус трека: **{document['status']}**. Отчёт кандидата: `{cand['report']['file']}` "
        f"(SHA-256 `{cand['report']['sha256'][:12]}…`).",
        "",
        "## 1. Журнал",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Строк в журнале всего | {journal['rows_total']} |",
        f"| Строк кандидата | {journal['rows_candidate']} |",
        f"| Первая строка | {journal['first_registered_at'] or '—'} |",
        f"| Последняя строка | {journal['last_registered_at'] or '—'} "
        f"({journal['last_vintage_week'] or 'anchor'}) |",
        f"| Дней с последней строки | {journal['days_since_last_row'] if journal['days_since_last_row'] is not None else '—'} |",
        "",
        "## 2. Прогресс к гейту C6",
        "",
        "| Критерий | Текущее | Требуется |",
        "|---|---|---|",
        f"| Разрешённых прогнозов | {c6['n_resolved']} | {c6['min_forecasts']} |",
        f"| Различных целевых периодов | {c6['distinct_target_periods']} | "
        f"{c6['min_distinct_target_periods']} |",
        f"| Покрытие интервалов | {'в допуске' if c6['coverage_met'] else 'вне допуска'} | допуск |",
        f"| Кумулятивная отн. потеря | "
        + (", ".join(f"{loss} {value:.4f}" for loss, value in c6["cumulative_relative_loss"].items())
           or "—")
        + " | ≤ 1,00 по каждому loss |",
        "",
        "## 3. Алерты",
        "",
    ]
    if document["alerts"]:
        for alert in document["alerts"]:
            lines.append(f"- **{alert['severity']}** `{alert['code']}` — {alert['message']}")
    else:
        lines.append("- алертов нет")
    projection = document["projection"]
    lines += [
        "",
        "## 4. Прогноз календаря",
        "",
        f"- Осталось строк до 12: {projection['rows_remaining']}; при ~"
        f"{projection['release_weeks_per_month']} релизных недель в месяц — около "
        f"{projection['months_to_min_forecasts']} мес. до 12 строк.",
        f"- Ориентировочная дата гейта: {projection['estimated_gate_date'] or '—'}.",
        f"- Допущение: {projection['assumption']}.",
        "",
        "## 5. Что это значит",
        "",
        f"- {cand['id']} находится в SHADOW_TEST: прогнозы пишутся в журнал, но допуска нет; "
        "официальные утверждения о качестве — только по гейту C6 и после него.",
        "- Регламент, пороги и правила остановки: docs/119.",
        "- Сырых сторонних данных и значений фактических наблюдений здесь нет.",
        "",
        "---",
        "",
        "Сгенерировано `tools/shadow_track.py`; журнал не изменяется, протоколы не меняются.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shadow-track monitor (SHADOW_TEST -> C6 gate)")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--date", default=None, help="report date YYYY-MM-DD (default: today)")
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = Path(args.out_dir) if args.out_dir else root / "build" / "shadow"
    today = date.fromisoformat(args.date) if args.date else None
    try:
        document = build_status(root, args.candidate, args.target, args.horizon,
                                today=today, generated_at=args.generated_at)
    except ShadowTrackError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(document)
    latest = out_dir / "shadow_status.json"
    latest.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8", newline="\n")
    (out_dir / f"{document['date']}.json").write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                                                     encoding="utf-8", newline="\n")
    (out_dir / f"{document['date']}.md").write_text(markdown, encoding="utf-8", newline="\n")
    print(json.dumps({"ok": True, "out_dir": str(out_dir), "date": document["date"],
                      "candidate": document["candidate"]["id"], "status": document["status"],
                      "n_resolved": document["c6"]["n_resolved"],
                      "alerts": [a["code"] for a in document["alerts"]],
                      "files": [f"{document['date']}.md", f"{document['date']}.json",
                                "shadow_status.json"]}, ensure_ascii=False))
    return 2 if document["status"] in ("ALERT",) else 0


if __name__ == "__main__":
    raise SystemExit(main())
