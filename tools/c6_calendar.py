#!/usr/bin/env python3
"""C6 calendar: expected release weeks and dates to the C6 gate of the shadow track (docs/139).

The tool reads the *derived* status document of :mod:`shadow_track`
(``build/shadow/shadow_status.json``) and projects the future **release weeks** in which the
candidate may be registered, the date at which the 12th forecast resolves and the date at which the
4th distinct target period resolves, plus the resulting gate estimate.  It never writes the journal,
never invents a registered row and never changes a protocol: every projected week is explicitly
marked ``PROJECTED_NOT_REGISTERED`` and the source journal counts come from the status document.

Rules encoded (docs/119, protocol v5 C6):
  * a row may be registered only in a **release week** (a week with a first data release for the
    target); the status projection supplies the average ``release_weeks_per_month`` (2.5);
  * a registered row resolves only when the fact for its target period is published: the status
    assumption is about one month of resolution lag, used here as ``resolution_lag_days`` (30);
  * C6 needs **>= 12 resolved forecasts** and **>= 4 distinct target periods**, both measured on
    resolved rows; the loss/coverage gates need the same resolved sample;
  * a forecast is point-in-time: the origin is the week of registration, the target period is the
    month after it for T1 (CPI y/y, h=1) — the projection labels the *estimated* target period
    only, it is not a journal value.

Determinism: the anchor is ``status.date`` (or ``--date``); there is no clock read, no timestamp in
the document, and the same inputs produce byte-identical JSON/MD.

Fail-closed: a missing/unreadable status, a broken schema, missing ``c6``/``journal``/``projection``
blocks, inconsistent counts (``distinct > resolved``, negative counters, ``rows_candidate >
rows_total``), or a non-positive release cadence raise :class:`C6CalendarError` instead of a guessed
calendar.

CLI:
    python tools/c6_calendar.py                      # human-readable summary
    python tools/c6_calendar.py --json               # machine-readable document on stdout
    python tools/c6_calendar.py --out build/shadow   # writes c6_calendar.json + c6_calendar.md
    python tools/c6_calendar.py --date 2026-12-01 --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS = ROOT / "build" / "shadow" / "shadow_status.json"
SCHEMA = "AMFE-C6-CALENDAR/1.0.0"
DEFAULT_OUT_DIR = ROOT / "build" / "shadow"
CALENDAR_JSON = "c6_calendar.json"
CALENDAR_MD = "c6_calendar.md"
DEFAULT_RESOLUTION_LAG_DAYS = 30
PROJECTION_SHIFT_WARN_DAYS = 7
MAX_PROJECTED_WEEKS = 200


class C6CalendarError(RuntimeError):
    """Missing/broken status document or a non-projectable configuration."""


# --------------------------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def next_release_week(anchor: date, step: int, cadence_days: float) -> date:
    """Monday of the ``step``-th expected release week after ``anchor`` (deterministic schedule).

    The average spacing between release weeks is ``30 / release_weeks_per_month`` days (12 days at
    the documented 2.5 weeks/month); each expected date is snapped to the Monday of its ISO week.
    This is a cadence model, not a release-calendar feed: the real BLS/BEA weeks are confirmed by a
    human each Friday (docs/119) and can shift the schedule.
    """
    offset = int(round(step * float(cadence_days)))
    return monday_of(anchor + timedelta(days=offset))


def _number(value, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise C6CalendarError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise C6CalendarError(f"{name} must be finite, got {value!r}")
    if minimum is not None and number < minimum:
        raise C6CalendarError(f"{name} must be >= {minimum}, got {number}")
    return number


def _int(value, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise C6CalendarError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise C6CalendarError(f"{name} must be >= {minimum}, got {value}")
    return value


def _date(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise C6CalendarError(f"{name} is not an ISO date: {value!r}") from exc


# ------------------------------------------------------------------------------------ status load
def load_status(path: Path = DEFAULT_STATUS) -> dict:
    """Read and structurally validate the shadow status document (fail-closed)."""
    path = Path(path)
    if not path.is_file():
        raise C6CalendarError(f"shadow status not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise C6CalendarError(f"cannot read shadow status {path}: {exc!r}") from exc
    validate_status(document)
    return document


def validate_status(document) -> None:
    """Structural validation of the fields the calendar depends on."""
    if not isinstance(document, dict):
        raise C6CalendarError("status document must be a JSON object")
    missing = [key for key in ("date", "journal", "c6", "projection", "rules") if key not in document]
    if missing:
        raise C6CalendarError(f"status document is missing keys: {missing}")
    _date(document["date"], "date")
    journal = document["journal"]
    c6 = document["c6"]
    projection = document["projection"]
    if not isinstance(journal, dict) or not isinstance(c6, dict) or not isinstance(projection, dict):
        raise C6CalendarError("journal, c6 and projection must be JSON objects")
    rows_total = _int(journal.get("rows_total"), "journal.rows_total", minimum=0)
    rows_candidate = _int(journal.get("rows_candidate"), "journal.rows_candidate", minimum=0)
    if rows_candidate > rows_total:
        raise C6CalendarError("journal.rows_candidate cannot exceed journal.rows_total")
    n_resolved = _int(c6.get("n_resolved"), "c6.n_resolved", minimum=0)
    min_forecasts = _int(c6.get("min_forecasts"), "c6.min_forecasts", minimum=1)
    distinct = _int(c6.get("distinct_target_periods"), "c6.distinct_target_periods", minimum=0)
    min_distinct = _int(c6.get("min_distinct_target_periods"),
                        "c6.min_distinct_target_periods", minimum=1)
    if distinct > rows_candidate:
        raise C6CalendarError(
            f"c6.distinct_target_periods ({distinct}) cannot exceed journal.rows_candidate "
            f"({rows_candidate})")
    if n_resolved > rows_candidate and rows_candidate >= 0:
        raise C6CalendarError(
            f"c6.n_resolved ({n_resolved}) cannot exceed journal.rows_candidate ({rows_candidate})")
    release_weeks = _number(projection.get("release_weeks_per_month"),
                            "projection.release_weeks_per_month", minimum=0.01)
    if release_weeks > 23.0:
        raise C6CalendarError("projection.release_weeks_per_month is implausible (> 23)")
    if not isinstance(document["rules"], dict):
        raise C6CalendarError("rules must be a JSON object")
    for key in ("days_since_last_row",):
        value = journal.get(key)
        if value is not None:
            _int(value, f"journal.{key}", minimum=0)


# --------------------------------------------------------------------------------------- calendar
def build_calendar(status: dict, *, today: date | None = None,
                   resolution_lag_days: int = DEFAULT_RESOLUTION_LAG_DAYS) -> dict:
    """Project the release weeks, criterion dates and the gate estimate from a status document."""
    validate_status(status)
    anchor = today or _date(status["date"], "date")
    lag = _int(resolution_lag_days, "resolution_lag_days", minimum=0)
    journal = status["journal"]
    c6 = status["c6"]
    projection = status["projection"]
    rules = status["rules"]

    rows_total = journal["rows_total"]
    rows_candidate = journal["rows_candidate"]
    n_resolved = c6["n_resolved"]
    min_forecasts = c6["min_forecasts"]
    distinct = c6["distinct_target_periods"]
    min_distinct = c6["min_distinct_target_periods"]
    release_weeks = float(projection["release_weeks_per_month"])
    cadence_days = 30.0 / release_weeks
    pending_unresolved = max(0, rows_candidate - n_resolved)
    registrations_needed = max(0, min_forecasts - rows_candidate)
    warnings: list[dict] = []

    def warn(code: str, severity: str, message: str) -> None:
        warnings.append({"code": code, "severity": severity, "message": message})

    # -- expected release weeks for the registrations still missing ----------------------
    weeks: list[dict] = []
    for step in range(1, registrations_needed + 1):
        week_start = next_release_week(anchor, step, cadence_days)
        origin_month = week_start.replace(day=1)
        if origin_month.month == 12:
            target_month = date(origin_month.year + 1, 1, 1)
        else:
            target_month = date(origin_month.year, origin_month.month + 1, 1)
        weeks.append({
            "sequence": step,
            "week": iso_week_label(week_start),
            "week_start": week_start.isoformat(),
            "week_index_from_anchor": int(round(step * cadence_days / 7.0)),
            "purpose": "REGISTRATION_WINDOW",
            "row_status": "PROJECTED_NOT_REGISTERED",
            "estimated_target_period": f"{target_month.year:04d}-{target_month.month:02d}",
            "resolves_at": (week_start + timedelta(days=lag)).isoformat(),
        })

    # -- resolution events: pending registered rows + projected registrations -----------
    pending_resolution = anchor + timedelta(days=lag)
    resolution_events = [pending_resolution for _ in range(pending_unresolved)]
    resolution_events += [week_start + timedelta(days=lag)
                          for week_start in (next_release_week(anchor, step, cadence_days)
                                             for step in range(1, registrations_needed + 1))]
    resolution_events.sort()
    remaining_to_resolve = max(0, min_forecasts - n_resolved)
    if remaining_to_resolve == 0:
        date_12th_resolved = anchor
    elif resolution_events:
        if len(resolution_events) >= remaining_to_resolve:
            date_12th_resolved = resolution_events[remaining_to_resolve - 1]
        else:
            # all projected rows are already registered but unresolved: use one more window
            last_week = next_release_week(anchor, registrations_needed + 1, cadence_days)
            date_12th_resolved = last_week + timedelta(days=lag)
    else:
        date_12th_resolved = None

    # -- 4 distinct target periods: one new period per new origin month ------------------
    periods_needed = max(0, min_distinct - distinct)
    date_distinct_resolved = None
    if periods_needed == 0:
        date_distinct_resolved = anchor
    else:
        seen_months = {anchor.replace(day=1)}
        found_week = None
        for step in range(1, MAX_PROJECTED_WEEKS + 1):
            week_start = next_release_week(anchor, step, cadence_days)
            month = week_start.replace(day=1)
            if month in seen_months:
                continue
            seen_months.add(month)
            if len(seen_months) - 1 >= periods_needed:
                found_week = week_start
                break
        if found_week is None:
            raise C6CalendarError("could not project the distinct target periods within the cap")
        # the distinct target period is counted on a *registered* row (journal rule), so the date
        # is the registration week, not registration + resolution lag
        date_distinct_resolved = found_week

    # -- gate estimate -------------------------------------------------------------------
    criteria_dates = [value for value in (date_12th_resolved, date_distinct_resolved)
                      if value is not None]
    gate_reached = bool(n_resolved >= min_forecasts and distinct >= min_distinct)
    if gate_reached:
        gate_date = anchor
    elif criteria_dates:
        gate_date = max(criteria_dates)
    else:
        gate_date = None
    status_projection_date = projection.get("estimated_gate_date")
    shift_days = None
    if gate_date is not None and status_projection_date:
        shift_days = (gate_date - _date(status_projection_date, "estimated_gate_date")).days
        if abs(shift_days) > PROJECTION_SHIFT_WARN_DAYS:
            direction = "позже" if shift_days > 0 else "раньше"
            explained = shift_days < 0 and pending_unresolved > 0
            warn("PROJECTION_SHIFT", "INFO" if explained else "WARN",
                 f"календарь даёт дату гейта {gate_date.isoformat()} — на {abs(shift_days)} дней "
                 f"{direction} ориентира статуса {status_projection_date}"
                 + ("; разница объяснима: pending-строки уже зарегистрированы и учтены как "
                    "разрешаемые" if explained else "; ориентир пересмотреть"))
    weeks_to_12th = None
    if date_12th_resolved is not None:
        weeks_to_12th = max(0, int(round((date_12th_resolved - anchor).days / 7.0)))
    months_to_gate = None
    if gate_date is not None:
        months_to_gate = round((gate_date - anchor).days / 30.0, 1)

    # -- warnings ------------------------------------------------------------------------
    days_since = journal.get("days_since_last_row")
    if days_since is not None:
        if days_since > int(math.ceil(2.0 * cadence_days)):
            warn("MISSED_RELEASE_ALERT", "ALERT",
                 f"нет новых строк {days_since} дн. (> {int(math.ceil(2.0 * cadence_days))}), "
                 "пропущено больше одной релизной недели")
        elif days_since > int(math.ceil(cadence_days)):
            warn("MISSED_RELEASE_WARN", "WARN",
                 f"нет новых строк {days_since} дн. (> {int(math.ceil(cadence_days))}): "
                 "вероятно, релизная неделя пропущена")
    if rows_candidate == 0:
        warn("NO_ROWS", "WARN", "в журнале нет ни одной строки кандидата")
    if pending_unresolved > 0:
        warn("PENDING_RESOLUTION", "INFO",
             f"{pending_unresolved} зарегистрированных строк(и) ждут публикации факта; "
             "они учтены в календаре, но ещё не разрешены")
    if not gate_reached and periods_needed > 0 and date_12th_resolved is not None \
            and date_distinct_resolved is not None and date_distinct_resolved > date_12th_resolved:
        warn("PERIODS_AFTER_COUNT", "INFO",
             "12-й прогноз разрешится раньше 4-го целевого периода: гейт ждёт периодов")
    if not gate_reached and rows_candidate == 0:
        warn("NO_REGISTRATION", "WARN", "ни одной строки не зарегистрировано: календарь "
                                         "начинается с первой релизной недели")
    if journal.get("last_vintage_week"):
        expected_current = iso_week_label(anchor)
        if str(journal["last_vintage_week"]) < expected_current and days_since is not None \
                and days_since > int(math.ceil(cadence_days)):
            warn("STALE_STATUS", "INFO",
                 f"последняя винтажная неделя {journal['last_vintage_week']} старше текущей "
                 f"{expected_current}; статус мог не обновиться")
    if gate_reached:
        warn("GATE_REACHED", "INFO", "C6-критерии по количеству и периодам выполнены; "
                                     "дата гейта — дата статуса, требуется проверка loss/coverage")

    rules_subset = {
        "registration": "только недели с первым релизом данных по цели (release weeks)",
        "point_in_time": "строка фиксирует origin и target period; значения не добавляются задним числом",
        "suspend_loss": rules.get("suspend_loss"),
        "warn_loss": rules.get("warn_loss"),
        "suspend_cadence_days": rules.get("suspend_cadence_days"),
        "reference": rules.get("reference", "docs/119, protocol v5 (C6)"),
    }
    return {
        "schema": SCHEMA,
        "source": {
            "status": "build/shadow/shadow_status.json",
            "status_sha256": None,
            "status_date": status["date"],
            "anchor_date": anchor.isoformat(),
            "candidate": (status.get("candidate") or {}).get("id"),
            "pair": status.get("pair"),
        },
        "progress": {
            "n_resolved": n_resolved,
            "min_forecasts": min_forecasts,
            "count_met": n_resolved >= min_forecasts,
            "distinct_target_periods": distinct,
            "min_distinct_target_periods": min_distinct,
            "distinct_periods_met": distinct >= min_distinct,
            "rows_total": rows_total,
            "rows_candidate": rows_candidate,
            "pending_unresolved": pending_unresolved,
            "registrations_needed": registrations_needed,
        },
        "projection": {
            "release_weeks_per_month": release_weeks,
            "cadence_days": round(cadence_days, 3),
            "resolution_lag_days": lag,
            "weeks": weeks,
            "weeks_to_12th_resolved": weeks_to_12th,
            "date_12th_resolved": date_12th_resolved.isoformat() if date_12th_resolved else None,
            "distinct_periods_needed": periods_needed,
            "date_distinct_periods_resolved":
                date_distinct_resolved.isoformat() if date_distinct_resolved else None,
            "gate_date": gate_date.isoformat() if gate_date else None,
            "months_to_gate": months_to_gate,
            "gate_reached": gate_reached,
            "status_estimated_gate_date": status_projection_date,
            "shift_vs_status_days": shift_days,
            "estimate": True,
            "note": ("дата — оценка по средней частоте релизов и лагу разрешения; реальные "
                     "релизные недели подтверждает человек по календарю BLS/BEA (docs/119)"),
        },
        "warnings": warnings,
        "rules": rules_subset,
        "limitations": [
            "календарь — детерминированная оценка по средней частоте релизных недель, а не обещание "
            "даты; реальные релизы сдвигают недели",
            "строки не фабрикуются: projected-недели помечены PROJECTED_NOT_REGISTERED, значения "
            "прогнозов и фактов не создаются",
            "лаг разрешения принят ~30 дней (допущение статуса); фактическая дата публикации "
            "факта зависит от календаря статистики",
            "целевой период в projected-неделях — оценка (месяц после origin), не журнальное "
            "значение",
            "C6 дополнительно требует loss <= 1.00 и покрытие в допуске по разрешённой выборке; "
            "эти проверки выполняются только по фактам",
        ],
    }


def build_from_path(status_path: Path = DEFAULT_STATUS, *, today: date | None = None,
                    resolution_lag_days: int = DEFAULT_RESOLUTION_LAG_DAYS) -> dict:
    status = load_status(status_path)
    document = build_calendar(status, today=today, resolution_lag_days=resolution_lag_days)
    document["source"]["status"] = str(Path(status_path))
    document["source"]["status_sha256"] = sha256_file(Path(status_path))
    return document


# ---------------------------------------------------------------------------------------- render
def render_markdown(document: dict) -> str:
    progress = document["progress"]
    projection = document["projection"]
    source = document["source"]
    warnings = document["warnings"]
    lines = [
        f"# C6-календарь теневого трека ({source['anchor_date']})",
        "",
        f"Кандидат: `{source.get('candidate')}`; пара "
        f"`{(source.get('pair') or {}).get('target')}` / h="
        f"`{(source.get('pair') or {}).get('horizon')}`. Источник: `{source['status']}` "
        f"(SHA-256 `{(source.get('status_sha256') or '-')[:12]}…`).",
        "",
        "**Статус: оценка календаря, не обещание даты и не допуск.** Строки не фабрикуются: "
        "projected-недели — окна регистрации, журнал не изменяется.",
        "",
        "## 1. Текущий прогресс C6",
        "",
        "| Критерий | Текущее | Требуется | Выполнено |",
        "|---|---:|---:|---|",
        f"| Разрешённых прогнозов | {progress['n_resolved']} | {progress['min_forecasts']} | "
        f"{'да' if progress['count_met'] else 'нет'} |",
        f"| Различных целевых периодов | {progress['distinct_target_periods']} | "
        f"{progress['min_distinct_target_periods']} | "
        f"{'да' if progress['distinct_periods_met'] else 'нет'} |",
        f"| Строк кандидата в журнале | {progress['rows_candidate']} | — | — |",
        f"| Из них ждут факта | {progress['pending_unresolved']} | — | — |",
        "",
        f"Нужно зарегистрировать ещё **{progress['registrations_needed']}** строк(и) "
        f"(12-й прогноз разрешится через ~{projection['weeks_to_12th_resolved']} нед.).",
        "",
        "## 2. Ожидаемые недели регистрации",
        "",
    ]
    if projection["weeks"]:
        lines += ["| # | неделя | начало недели | оценка target period | разрешится к |",
                  "|---|---|---|---|---|"]
        for entry in projection["weeks"]:
            lines.append(f"| {entry['sequence']} | `{entry['week']}` | {entry['week_start']} | "
                         f"{entry['estimated_target_period']} | {entry['resolves_at']} |")
    else:
        lines.append("Регистрации не требуются: строк уже достаточно (или гейт достигнут).")
    lines += [
        "",
        "## 3. Даты достижения критериев",
        "",
        "| Событие | Дата (оценка) |",
        "|---|---|",
        f"| 12-й разрешённый прогноз | {projection['date_12th_resolved'] or '—'} |",
        f"| 4-й различный целевой период (строка зарегистрирована) | "
        f"{projection['date_distinct_periods_resolved'] or '—'} |",
        f"| Ориентир гейта C6 | **{projection['gate_date'] or '—'}** "
        f"(через ~{projection['months_to_gate']} мес.) |",
        f"| Ориентир из статуса | {projection['status_estimated_gate_date'] or '—'} |",
        f"| Сдвиг календаря против статуса | "
        f"{projection['shift_vs_status_days'] if projection['shift_vs_status_days'] is not None else '—'} дн. |",
        "",
        "## 4. Предупреждения",
        "",
    ]
    if warnings:
        for item in warnings:
            lines.append(f"- **{item['severity']}** `{item['code']}` — {item['message']}")
    else:
        lines.append("- предупреждений нет")
    lines += [
        "",
        "## 5. Правила и допущения",
        "",
        f"- частота релизных недель: {projection['release_weeks_per_month']} в месяц "
        f"(шаг ~{projection['cadence_days']} дн.); лаг разрешения "
        f"{projection['resolution_lag_days']} дн.",
        f"- регистрация: {document['rules']['registration']}; PIT: "
        f"{document['rules']['point_in_time']}",
        f"- пороги: loss > {document['rules']['suspend_loss']} — стоп, > "
        f"{document['rules']['warn_loss']} — предупреждение; cadence "
        f"{document['rules']['suspend_cadence_days']} дн.; ссылка: {document['rules']['reference']}",
        "",
        "## 6. Ограничения",
        "",
    ]
    lines += [f"- {item}" for item in document["limitations"]]
    lines += ["", "Сгенерировано `tools/c6_calendar.py`; журнал не изменяется.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- CLI
def _write(document: dict, out_path: Path) -> list[str]:
    out_path = Path(out_path)
    written = []
    if out_path.suffix.lower() == ".md":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_markdown(document), encoding="utf-8", newline="\n")
        return [str(out_path)]
    if out_path.suffix.lower() == ".json":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                            encoding="utf-8", newline="\n")
        return [str(out_path)]
    out_path.mkdir(parents=True, exist_ok=True)
    json_path = out_path / CALENDAR_JSON
    md_path = out_path / CALENDAR_MD
    json_path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                         encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(document), encoding="utf-8", newline="\n")
    written.extend([str(json_path), str(md_path)])
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="C6 calendar: expected release weeks and dates to the C6 gate (docs/139)")
    parser.add_argument("--status", default=str(DEFAULT_STATUS),
                        help="shadow status document (default build/shadow/shadow_status.json)")
    parser.add_argument("--date", default=None,
                        help="anchor date YYYY-MM-DD (default: status.date; keeps the run deterministic)")
    parser.add_argument("--resolution-lag-days", dest="resolution_lag_days", type=int,
                        default=DEFAULT_RESOLUTION_LAG_DAYS,
                        help="assumed days between registration and resolution (default 30)")
    parser.add_argument("--json", action="store_true", help="print the calendar document as JSON")
    parser.add_argument("--out", default=None,
                        help="write the calendar: directory (json+md), .json or .md path")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else None
    try:
        document = build_from_path(Path(args.status), today=today,
                                   resolution_lag_days=args.resolution_lag_days)
    except C6CalendarError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.out:
        document["written"] = _write(document, Path(args.out))
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1))
        return 0
    progress = document["progress"]
    projection = document["projection"]
    print(f"C6-календарь ({document['source']['anchor_date']}): "
          f"разрешено {progress['n_resolved']}/{progress['min_forecasts']}, "
          f"периодов {progress['distinct_target_periods']}/{progress['min_distinct_target_periods']}")
    print(f"  недель до 12-го: {projection['weeks_to_12th_resolved']}; "
          f"12-й разрешён: {projection['date_12th_resolved']}; "
          f"4-й период: {projection['date_distinct_periods_resolved']}")
    print(f"  ориентир гейта: {projection['gate_date']} "
          f"(через ~{projection['months_to_gate']} мес.; статус: "
          f"{projection['status_estimated_gate_date']})")
    for item in document["warnings"]:
        print(f"  [{item['severity']}] {item['code']}: {item['message']}")
    if args.out:
        for path in document.get("written", []):
            print(f"  записано: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
