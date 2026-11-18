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

# ------------------------------------------------------------------ operational C6 process (docs/139)
PROCESS_SCHEMA = "AMFE-C6-PROCESS/1.0.0"
AUDIT_SCHEMA = "AMFE-C6-AUDIT/1.0.0"
AUDIT_EVENTS = ("ROW_REGISTERED", "ROW_RESOLVED", "WEEK_CHECK")
DEFAULT_JOURNAL = ROOT / "registry" / "forecasts.jsonl"
DEFAULT_AUDIT = ROOT / "build" / "c6" / "c6_audit.jsonl"
DEFAULT_REPORT_DIR = ROOT / "build" / "c6"
REPORT_JSON = "c6_report.json"
REPORT_MD = "c6_report.md"
DEFAULT_CANDIDATE = "ENSEMBLE_REGIME_V4"
DEFAULT_TARGET = "T1_CPI_YOY"
DEFAULT_HORIZON = 1
MIN_FORECASTS = 12
MIN_DISTINCT_PERIODS = 4
CONTROL_ARMS = ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY", "OWN_NOWCAST_BRIDGE")
#: the comparators used by tools/registry_resolve.py for each loss family
COMPARATOR_ARMS = {"quantiles": ("EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"),
                   "event": ("EQUAL_WEIGHT_DENSITY",)}
UNRESOLVED_LATE_DAYS = 30
UNRESOLVED_ALERT_DAYS = 45


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


# ======================================================================= operational C6 process
def _repo_path(path: Path) -> str:
    try:
        return str(Path(path).relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + int(months)
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def _month_end(day: date) -> date:
    return _add_months(day.replace(day=1), 1) - timedelta(days=1)


def protocol_period(target: str, origin: str, horizon: int) -> dict:
    """Target calendar period of a registry row (protocol v4 ``target_period``), stdlib only.

    T1: y/y inflation of month ``t-1+h``; T2: growth of quarter ``Q(t)+h``; T3: the origin month.
    The function is a deterministic calendar mapping: it never reads data or the clock.
    """
    if target not in ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"):
        raise C6CalendarError(f"unknown target {target!r}")
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        raise C6CalendarError(f"horizon must be an integer, got {horizon!r}")
    parts = str(origin).split("-")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise C6CalendarError(f"origin is not YYYY-MM: {origin!r}")
    try:
        start = date(int(parts[0]), int(parts[1]), 1)
    except ValueError as exc:
        raise C6CalendarError(f"origin is not a month: {origin!r}") from exc
    if target == "T1_CPI_YOY":
        period_start = _add_months(start, horizon - 1)
        end = _month_end(period_start)
        return {"frequency": "M", "label": f"{period_start.year:04d}-{period_start.month:02d}",
                "end": end}
    if target == "T2_GDP_GROWTH":
        quarter_month = ((start.month - 1) // 3) * 3 + 1
        quarter_start = date(start.year, quarter_month, 1)
        period_start = _add_months(quarter_start, 3 * horizon)
        end = _month_end(_add_months(period_start, 2))
        quarter = (period_start.month - 1) // 3 + 1
        return {"frequency": "Q", "label": f"{period_start.year:04d}Q{quarter}", "end": end}
    end = _month_end(start)
    return {"frequency": "M", "label": f"{start.year:04d}-{start.month:02d}", "end": end}


def _registered_date(row: dict, name: str = "registered_at") -> date | None:
    value = row.get(name)
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise C6CalendarError(f"{name} is not an ISO date: {value!r}") from exc


def _row_week(row: dict) -> str | None:
    """ISO week of a row: the explicit vintage_week, else the week of registered_at."""
    label = row.get("vintage_week")
    if label:
        return str(label)
    registered = _registered_date(row)
    return iso_week_label(registered) if registered else None


def _identity(row: dict) -> tuple:
    return (row.get("candidate_id"), row.get("target"), row.get("horizon"),
            row.get("origin"), row.get("vintage_week"))


def _group_key(row: dict) -> tuple:
    return (row.get("target"), row.get("horizon"), row.get("origin"), row.get("vintage_week"))


def week_start_from_label(label: str) -> date:
    parts = str(label).split("-W")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise C6CalendarError(f"bad ISO week {label!r}; expected YYYY-Www")
    try:
        return date.fromisocalendar(int(parts[0]), int(parts[1]), 1)
    except ValueError as exc:
        raise C6CalendarError(f"bad ISO week {label!r}") from exc


# ------------------------------------------------------------------------------------------ audit
def _read_journal(journal: Path) -> list[dict]:
    """Read and validate the append-only journal; a broken file is fail-closed."""
    from registry import RegistryError, read_rows, validate_journal

    try:
        rows = read_rows(journal)
        if Path(journal).exists():
            _, problems = validate_journal(journal)
            if problems:
                raise C6CalendarError("journal is not valid: " + "; ".join(problems))
    except RegistryError as exc:
        raise C6CalendarError(f"journal is not readable: {exc}") from exc
    return rows


def _validate_audit_event(event) -> None:
    if not isinstance(event, dict):
        raise C6CalendarError("audit event must be a JSON object")
    if event.get("schema") != AUDIT_SCHEMA:
        raise C6CalendarError(f"audit event schema must be {AUDIT_SCHEMA}")
    if event.get("event") not in AUDIT_EVENTS:
        raise C6CalendarError(f"unknown audit event {event.get('event')!r}")
    _date(event.get("at"), "audit event at")
    if event["event"] == "WEEK_CHECK":
        week_start_from_label(event.get("week"))
    else:
        for field in ("candidate_id", "target", "horizon", "origin"):
            if field not in event:
                raise C6CalendarError(f"audit event {event['event']} is missing {field}")
        if event["event"] == "ROW_RESOLVED":
            _date(event.get("first_publication"), "audit first_publication")


def read_audit(path: Path = DEFAULT_AUDIT) -> list[dict]:
    """Read the append-only operational audit log; a broken line is fail-closed."""
    path = Path(path)
    if not path.is_file():
        return []
    events: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise C6CalendarError(f"{path}: audit line {number} is not JSON: {error}") from error
        try:
            _validate_audit_event(event)
        except C6CalendarError as error:
            raise C6CalendarError(f"{path}: audit line {number}: {error}") from error
        events.append(event)
    return events


def _audit_key(event: dict) -> tuple:
    if event["event"] == "WEEK_CHECK":
        return ("WEEK_CHECK", event["week"])
    return (event["event"], _identity(event))


def append_audit_events(events: list[dict], path: Path = DEFAULT_AUDIT) -> list[dict]:
    """Append audit events, skipping byte-identical identities already present (idempotent)."""
    path = Path(path)
    existing = read_audit(path)
    seen = {_audit_key(event) for event in existing}
    written: list[dict] = []
    for event in events:
        _validate_audit_event(event)
        key = _audit_key(event)
        if key in seen:
            continue
        seen.add(key)
        written.append(event)
    if written:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for event in written:
                handle.write(_canonical(event) + "\n")
    return written


# ------------------------------------------------------------------------------------ registration
def _normalise_records(payload) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("rows"), list):
            rows = payload["rows"]
        elif isinstance(payload.get("records"), list) \
                and all(isinstance(item, dict) and "quantiles_5_to_95_step_5" in item
                        or isinstance(item, dict) and "event_probability" in item
                        for item in payload["records"]):
            rows = payload["records"]
        else:
            raise C6CalendarError("records document has no full rows (expected 'rows' or 'records')")
    else:
        raise C6CalendarError("records document must be a JSON array or object")
    if not rows:
        raise C6CalendarError("records document is empty")
    if not all(isinstance(row, dict) for row in rows):
        raise C6CalendarError("every record must be a JSON object")
    return [dict(row) for row in rows]


def load_records(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        raise C6CalendarError(f"records file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise C6CalendarError(f"cannot read records {path}: {exc!r}") from exc
    return _normalise_records(payload)


def default_fact_lookup(target: str, origin: str, horizon: int) -> dict:
    """First-publication actual of the target period; the value is never reported.

    ``available`` says whether the local vintage archive exists at all, so an empty archive is not
    confused with an unpublished fact (both yield ``primary = None``).
    """
    name = {"T1_CPI_YOY": "CPIAUCSL", "T2_GDP_GROWTH": "GDPC1"}.get(target)
    available = True if name is None else (ROOT / "data" / "raw" / name).exists()
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    try:
        from amfe.evaluation.actuals import actual_pair
        pair = actual_pair(target, origin, int(horizon))
    except Exception as exc:  # pragma: no cover - data layer specific
        raise C6CalendarError(f"cannot read the first-publication actual: {exc!r}") from exc
    return {"primary": pair.get("primary"), "primary_vintage": pair.get("primary_vintage"),
            "available": available}


def _target_kind(target: str) -> str:
    return "event" if target == "T3_RECESSION" else "quantiles"


def _cached_fact_lookup(fact_lookup):
    """Memoise the fact lookup: a weekly batch has many rows for the same target period."""
    lookup = fact_lookup or default_fact_lookup
    cache: dict[tuple, dict] = {}

    def cached(target, origin, horizon):
        key = (target, str(origin), int(horizon))
        if key not in cache:
            cache[key] = lookup(target, origin, int(horizon))
        return cache[key]

    return cached


def register_week(records, *, journal: Path = DEFAULT_JOURNAL, audit: Path = DEFAULT_AUDIT,
                  as_of: date, fact_lookup=None, require_control: bool = True,
                  dry_run: bool = False) -> dict:
    """Register one weekly forecast row together with its control group (docs/139 runbook).

    Guards, all fail-closed: the row must carry ``vintage_week`` equal to the ISO week of ``as_of``
    and of ``registered_at`` (no backdating, no future rows); the fact of the target period must
    not be published (no look-ahead); a candidate row needs the comparator arms of its loss family
    in the same batch or already in the journal; append-only duplicates are either byte-identical
    (skipped) or refused.  Every appended row gets a ``ROW_REGISTERED`` audit event.
    """
    from registry import RegistryError, append_record, record_digest, validate_record

    as_of = _date(as_of, "as_of")
    rows = _normalise_records(records)
    problems = []
    for row in rows:
        problems.extend(f"{row.get('candidate_id')}/{row.get('target')}: {problem}"
                        for problem in validate_record(row))
    if problems:
        raise C6CalendarError("invalid record(s): " + "; ".join(problems))

    weeks = {row.get("vintage_week") for row in rows}
    if None in weeks or len(weeks) != 1:
        raise C6CalendarError("weekly registration needs exactly one explicit vintage_week in the batch")
    vintage_week = weeks.pop()
    week_start_from_label(vintage_week)
    if vintage_week != iso_week_label(as_of):
        raise C6CalendarError(
            f"vintage_week {vintage_week} is not the current ISO week {iso_week_label(as_of)}: "
            "backdating and future rows are forbidden")

    groups = {_group_key(row) for row in rows}
    if len(groups) != 1:
        raise C6CalendarError("one weekly batch must share target, horizon, origin and vintage_week")
    target, horizon, origin, _ = groups.pop()
    horizon = int(horizon)

    for row in rows:
        registered = _registered_date(row)
        if registered is not None:
            if iso_week_label(registered) != vintage_week:
                raise C6CalendarError(
                    f"registered_at {row.get('registered_at')} is outside vintage_week {vintage_week}")
            if registered > as_of:
                raise C6CalendarError(f"registered_at {row.get('registered_at')} is in the future")
        else:
            row["registered_at"] = as_of.isoformat()

    fact = (fact_lookup or default_fact_lookup)(target, origin, horizon)
    if fact.get("primary") is not None:
        raise C6CalendarError(
            f"{target} h={horizon} origin {origin}: the first-publication fact already exists "
            f"({fact.get('primary_vintage')}); registering after the fact is look-ahead")

    candidate_rows = [row for row in rows
                      if not row.get("control_arm") and row.get("candidate_id") not in CONTROL_ARMS]
    if not candidate_rows:
        raise C6CalendarError("the batch has no candidate row (only control arms)")

    existing_rows = _read_journal(journal)
    available_arms = {row.get("candidate_id") for row in existing_rows
                      if _group_key(row) == (target, horizon, origin, vintage_week)
                      and (row.get("control_arm") or row.get("candidate_id") in CONTROL_ARMS)}
    available_arms |= {row.get("candidate_id") for row in rows
                       if row.get("control_arm") or row.get("candidate_id") in CONTROL_ARMS}
    if require_control:
        required = COMPARATOR_ARMS[_target_kind(target)]
        missing = [arm for arm in required if arm not in available_arms]
        if missing:
            raise C6CalendarError(
                f"control group for {target} h={horizon} {origin} {vintage_week} is incomplete: "
                f"missing {missing}; register the comparator arms in the same batch or earlier")

    existing = {_identity(row): row for row in existing_rows}
    planned, skipped = [], []
    for row in rows:
        key = _identity(row)
        if key in existing:
            if record_digest(existing[key]) != record_digest(row):
                raise C6CalendarError(
                    f"identity {key} already exists with different content: append-only, use a "
                    "correction row")
            skipped.append(row)
            continue
        if any(_identity(item) == key for item in planned):
            raise C6CalendarError(f"duplicate identity inside the batch: {key}")
        planned.append(row)

    appended = []
    for row in planned:
        if dry_run:
            digest = record_digest(row)
        else:
            try:
                result = append_record(row, journal)
            except RegistryError as exc:
                raise C6CalendarError(f"journal refused the row: {exc}") from exc
            digest = result["digest"]
        appended.append({"candidate_id": row.get("candidate_id"), "target": row.get("target"),
                         "horizon": row.get("horizon"), "origin": row.get("origin"),
                         "vintage_week": row.get("vintage_week"),
                         "control_arm": bool(row.get("control_arm")), "digest": digest})

    events = [{"schema": AUDIT_SCHEMA, "event": "ROW_REGISTERED", "at": as_of.isoformat(),
               "candidate_id": row.get("candidate_id"), "target": row.get("target"),
               "horizon": row.get("horizon"), "origin": row.get("origin"),
               "vintage_week": row.get("vintage_week"),
               "control_arm": bool(row.get("control_arm")), "digest": digest}
              for row, digest in zip(planned, [item["digest"] for item in appended])]
    written = [] if dry_run else append_audit_events(events, audit)
    return {"appended": appended, "skipped": [
                {"candidate_id": row.get("candidate_id"), "target": row.get("target"),
                 "horizon": row.get("horizon"), "origin": row.get("origin"),
                 "vintage_week": row.get("vintage_week")} for row in skipped],
            "journal": _repo_path(journal), "audit": _repo_path(audit),
            "audit_events_written": len(written), "dry_run": bool(dry_run),
            "group": {"target": target, "horizon": horizon, "origin": origin,
                      "vintage_week": vintage_week}}


# ------------------------------------------------------------------------------------- resolution
def resolve_journal(*, journal: Path = DEFAULT_JOURNAL, audit: Path = DEFAULT_AUDIT,
                    as_of: date, fact_lookup=None, persist: bool = False) -> dict:
    """Resolve rows whose first publication happened on or before ``as_of`` (no look-ahead).

    A row is resolved only when the first archived vintage containing the target period is dated
    on or before ``as_of``; a fact whose first publication is later is counted as blocked.  The
    audit stores the identity and the first-publication date, never the actual value.
    """
    as_of = _date(as_of, "as_of")
    rows = _read_journal(journal)
    events = read_audit(audit)
    already = {_identity(event) for event in events if event["event"] == "ROW_RESOLVED"}
    lookup = _cached_fact_lookup(fact_lookup)
    resolved, unresolved, blocked, future = [], [], [], []
    available = True
    for row in rows:
        target, origin, horizon = row.get("target"), row.get("origin"), int(row.get("horizon"))
        fact = lookup(target, origin, horizon)
        available = available and bool(fact.get("available", True))
        primary = fact.get("primary")
        if primary is None:
            unresolved.append(_identity(row))
            continue
        published = fact.get("primary_vintage")
        if not published:
            raise C6CalendarError(
                f"first-publication date is missing for {_identity(row)}; cannot prove no look-ahead")
        published_day = _date(published, "primary_vintage")
        if published_day > as_of:
            future.append(_identity(row))
            continue
        resolved.append(_identity(row))
        if _identity(row) not in already:
            blocked.append({"schema": AUDIT_SCHEMA, "event": "ROW_RESOLVED",
                            "at": as_of.isoformat(),
                            "candidate_id": row.get("candidate_id"), "target": target,
                            "horizon": horizon, "origin": origin,
                            "vintage_week": row.get("vintage_week"),
                            "first_publication": published_day.isoformat()})
    written = append_audit_events(blocked, audit) if persist else []
    return {"rows": len(rows), "resolved": len(resolved), "newly_resolved": len(blocked),
            "unresolved": len(unresolved), "future_fact_blocked": len(future),
            "data_available": available, "events": blocked,
            "written": len(written)}


# ----------------------------------------------------------------------------------------- report
def _pair_rows(rows: list[dict], candidate: str, target: str, horizon: int) -> list[dict]:
    return [row for row in rows
            if row.get("candidate_id") == candidate and row.get("target") == target
            and int(row.get("horizon", -1)) == int(horizon)]


def _control_groups(rows: list[dict], candidate: str, target: str, horizon: int) -> dict:
    pair = [row for row in rows if row.get("target") == target
            and int(row.get("horizon", -1)) == int(horizon)]
    groups: dict[tuple, dict] = {}
    for row in pair:
        key = _group_key(row)
        group = groups.setdefault(key, {"candidate": 0, "arms": set()})
        if row.get("control_arm") or row.get("candidate_id") in CONTROL_ARMS:
            group["arms"].add(row.get("candidate_id"))
        elif row.get("candidate_id") == candidate:
            group["candidate"] += 1
    required = COMPARATOR_ARMS[_target_kind(target)]
    complete, incomplete = [], []
    for key in sorted(groups, key=lambda item: tuple("" if part is None else str(part)
                                                     for part in item)):
        group = groups[key]
        if not group["candidate"]:
            continue
        missing = [arm for arm in required if arm not in group["arms"]]
        entry = {"target": key[0], "horizon": key[1], "origin": key[2],
                 "vintage_week": key[3], "candidate_rows": group["candidate"],
                 "arms": sorted(group["arms"]), "missing_arms": missing}
        (incomplete if missing else complete).append(entry)
    return {"complete": not incomplete, "groups": len(complete) + len(incomplete),
            "complete_groups": complete, "incomplete": incomplete,
            "required_arms": list(required)}


def _week_grid(start: date, end: date) -> list[date]:
    weeks, cursor = [], monday_of(start)
    stop = monday_of(end)
    while cursor <= stop:
        weeks.append(cursor)
        cursor += timedelta(days=7)
    return weeks


def build_process_report(*, journal: Path = DEFAULT_JOURNAL, audit: Path = DEFAULT_AUDIT,
                         as_of: date, candidate: str = DEFAULT_CANDIDATE,
                         target: str = DEFAULT_TARGET, horizon: int = DEFAULT_HORIZON,
                         fact_lookup=None) -> dict:
    """Operational C6 report: 12/4 counters, week coverage, control group, audit and alerts.

    Counts are taken from the journal and the first-publication facts; the loss and interval
    coverage checks stay with ``tools/registry_resolve.py`` (they need the evaluated sample) and
    the report says so instead of implying a C6 pass.
    """
    as_of = _date(as_of, "as_of")
    horizon = _int(horizon, "horizon", minimum=0)
    rows = _read_journal(journal)
    events = read_audit(audit)
    pair_rows = _pair_rows(rows, candidate, target, horizon)
    warnings: list[dict] = []

    def warn(code: str, severity: str, message: str) -> None:
        warnings.append({"code": code, "severity": severity, "message": message})

    resolved_rows, pending_rows, future_rows = [], [], []
    available = True
    lookup = _cached_fact_lookup(fact_lookup)
    for row in pair_rows:
        fact = lookup(target, row.get("origin"), horizon)
        available = available and bool(fact.get("available", True))
        primary = fact.get("primary")
        if primary is None:
            pending_rows.append(row)
            continue
        published = fact.get("primary_vintage")
        if not published:
            raise C6CalendarError(
                f"first-publication date is missing for {_identity(row)}; cannot prove no look-ahead")
        if _date(published, "primary_vintage") > as_of:
            future_rows.append(row)
        else:
            resolved_rows.append(row)

    periods_registered = {protocol_period(target, row.get("origin"), horizon)["label"]
                          for row in pair_rows}
    periods_resolved = {protocol_period(target, row.get("origin"), horizon)["label"]
                        for row in resolved_rows}
    n_resolved = len(resolved_rows)
    distinct = len(periods_registered)
    counts_met = n_resolved >= MIN_FORECASTS and distinct >= MIN_DISTINCT_PERIODS
    status_line = (f"{n_resolved}/{MIN_FORECASTS} прогнозов, "
                   f"{distinct}/{MIN_DISTINCT_PERIODS} периодов")
    status = ("COUNTS_MET" if counts_met else
              "STARTING" if n_resolved == 0 else "IN_PROGRESS")

    # -- weeks: rows, checks and process gaps ------------------------------------------------
    checked_weeks = {event["week"] for event in events if event["event"] == "WEEK_CHECK"}
    audit_days = sorted(_date(event["at"], "at") for event in events)
    row_days = sorted(_registered_date(row) for row in pair_rows if _registered_date(row))
    history = [day for day in (audit_days[0] if audit_days else None,
                               row_days[0] if row_days else None) if day is not None]
    history_start = min(history) if history else as_of
    if history_start > as_of:
        raise C6CalendarError("journal/audit history starts after the report date")
    by_week: dict[str, list[dict]] = {}
    for row in pair_rows:
        label = _row_week(row)
        if label is not None:
            by_week.setdefault(label, []).append(row)
    first_audit_week = iso_week_label(audit_days[0]) if audit_days else None
    weeks, missed, missed_checks, streak, max_streak = [], [], [], 0, 0
    for start in _week_grid(history_start, as_of):
        label = iso_week_label(start)
        end = start + timedelta(days=6)
        completed = end < as_of
        rows_week = by_week.get(label, [])
        checked = label in checked_weeks
        missing_row = completed and not rows_week
        missing_check = (completed and first_audit_week is not None
                         and label >= first_audit_week and not checked)
        if missing_row:
            missed.append(label)
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        if missing_check:
            missed_checks.append(label)
        if not completed:
            state = "CURRENT"
        elif rows_week and checked:
            state = "COMPLETE"
        elif rows_week:
            state = "NO_CHECK"
        elif checked:
            state = "NO_ROW"
        else:
            state = "MISSED"
        weeks.append({"week": label, "week_start": start.isoformat(), "week_end": end.isoformat(),
                      "status": state, "candidate_rows": len(rows_week),
                      "control_rows": len([row for row in rows
                                           if _row_week(row) == label
                                           and row.get("target") == target
                                           and int(row.get("horizon", -1)) == int(horizon)
                                           and (row.get("control_arm")
                                                or row.get("candidate_id") in CONTROL_ARMS)]),
                      "checked": checked})

    # -- control group -----------------------------------------------------------------------
    control = _control_groups(rows, candidate, target, horizon)

    # -- unresolved aging --------------------------------------------------------------------
    late_periods, overdue_periods = [], []
    if available:
        for row in pending_rows:
            period = protocol_period(target, row.get("origin"), horizon)
            late_day = period["end"] + timedelta(days=UNRESOLVED_LATE_DAYS)
            alert_day = period["end"] + timedelta(days=UNRESOLVED_ALERT_DAYS)
            entry = {"origin": row.get("origin"), "vintage_week": row.get("vintage_week"),
                     "period": period["label"], "period_end": period["end"].isoformat(),
                     "days_since_period_end": max(0, (as_of - period["end"]).days)}
            if as_of > alert_day:
                overdue_periods.append(entry)
            elif as_of > late_day:
                late_periods.append(entry)

    # -- alerts ------------------------------------------------------------------------------
    if not pair_rows:
        warn("NO_ROWS", "WARN", f"в журнале нет строк кандидата {candidate} для {target} h={horizon}")
    if not available:
        warn("FACTS_UNAVAILABLE", "WARN",
             "локальный винтажный архив отсутствует: разрешение и просрочка не проверяются "
             "(данные не выдаются за неопубликованный факт)")
    if not events:
        warn("NO_AUDIT", "INFO", "аудит-лог пуст: процесс ещё не запускался, история недель не "
                                 "контролируется (данных для статуса процесса пока нет)")
    if max_streak >= 2:
        warn("MISSED_WEEK_ALERT", "ALERT",
             f"{max_streak} завершённых недель подряд без строки кандидата "
             f"({', '.join(missed[:6])}): проверить календарь BLS/BEA; если недели были "
             "релизными — это пропуск процесса")
    elif max_streak == 1:
        warn("MISSED_WEEK", "WARN",
             f"в завершённой неделе {missed[0]} нет строки кандидата; если неделя была релизной — "
             "это пропуск (проверить календарь BLS/BEA)")
    if missed_checks:
        warn("MISSED_CHECK", "ALERT",
             f"нет недельной проверки (WEEK_CHECK) за {', '.join(missed_checks[:6])}: "
             "еженедельный цикл не выполнялся")
    if not control["complete"]:
        missing = "; ".join(f"{item['origin']}/{item['vintage_week']}: {item['missing_arms']}"
                            for item in control["incomplete"][:4])
        warn("CONTROL_GROUP_MISSING", "ALERT",
             f"контрольная группа неполная для {len(control['incomplete'])} групп(ы): {missing}; "
             "C6-потери сравнивать не с чем")
    if overdue_periods:
        warn("UNRESOLVED_OVERDUE", "ALERT",
             f"{len(overdue_periods)} строк(и) не разрешены позже {UNRESOLVED_ALERT_DAYS} дн. после "
             f"конца целевого периода (самый старый: {overdue_periods[0]['period']})")
    if late_periods:
        warn("UNRESOLVED_LATE", "WARN",
             f"{len(late_periods)} строк(и) не разрешены через {UNRESOLVED_LATE_DAYS}+ дн. после "
             "конца целевого периода: проверить публикацию факта")
    if future_rows:
        warn("FUTURE_FACT_BLOCKED", "INFO",
             f"{len(future_rows)} строк(и) имеют первую публикацию позже отчётной даты: они не "
             "разрешены (no look-ahead)")
    if counts_met:
        warn("COUNTS_MET", "INFO",
             "12/4 по количеству и периодам выполнены; loss/coverage проверяются отдельно "
             "по разрешённой выборке (tools/registry_resolve.py)")
    if pending_rows and not available:
        warn("PENDING_UNVERIFIED", "INFO",
             f"{len(pending_rows)} строк(и) ждут факта; без архива статус публикации не подтверждён")

    return {
        "schema": PROCESS_SCHEMA,
        "as_of": as_of.isoformat(),
        "week": iso_week_label(as_of),
        "pair": {"candidate": candidate, "target": target, "horizon": horizon},
        "source": {"journal": _repo_path(journal),
                   "journal_sha256": sha256_file(journal) if journal.exists() else None,
                   "audit": _repo_path(audit),
                   "audit_sha256": sha256_file(audit) if audit.exists() else None,
                   "audit_events": len(events)},
        "c6": {"n_resolved": n_resolved, "min_forecasts": MIN_FORECASTS,
               "counts_met": n_resolved >= MIN_FORECASTS,
               "distinct_target_periods": distinct,
               "min_distinct_target_periods": MIN_DISTINCT_PERIODS,
               "distinct_periods_met": distinct >= MIN_DISTINCT_PERIODS,
               "distinct_resolved_periods": len(periods_resolved),
               "pending_unresolved": len(pending_rows),
               "future_fact_blocked": len(future_rows),
               "status": status, "status_line": status_line,
               "status_12_4": {"forecasts": f"{n_resolved}/{MIN_FORECASTS}",
                               "periods": f"{distinct}/{MIN_DISTINCT_PERIODS}"},
               "counts_and_periods_met": counts_met,
               "basis": "registered_rows",
               "loss_coverage_checked": False,
               "gate": ("COUNTS_MET_LOSS_PENDING" if counts_met else "NOT_MET")},
        "weeks": {"weeks": weeks, "completed": sum(1 for week in weeks
                                                   if week["status"] != "CURRENT"),
                  "with_rows": sum(1 for week in weeks if week["candidate_rows"]),
                  "missed": missed, "missed_checks": missed_checks,
                  "max_consecutive_missed": max_streak,
                  "audit_started": bool(events)},
        "control_group": {"complete": control["complete"], "groups": control["groups"],
                          "required_arms": control["required_arms"],
                          "incomplete": control["incomplete"]},
        "unresolved": {"pending": len(pending_rows), "late": late_periods,
                       "overdue": overdue_periods, "facts_available": available},
        "alerts": warnings,
        "raw_values": False,
        "rules": {"min_forecasts": MIN_FORECASTS,
                  "min_distinct_periods": MIN_DISTINCT_PERIODS,
                  "control_arms": list(COMPARATOR_ARMS[_target_kind(target)]),
                  "unresolved_late_days": UNRESOLVED_LATE_DAYS,
                  "unresolved_alert_days": UNRESOLVED_ALERT_DAYS,
                  "reference": "docs/139 runbook, docs/119, protocol v5 (C6)"},
        "limitations": [
            "отчёт считает 12/4 по журналу и первой публикации факта; loss/coverage не "
            "пересчитываются — их даёт tools/registry_resolve.py по разрешённой выборке",
            "неделя считается пропущенной по отсутствию строки кандидата и/или WEEK_CHECK; "
            "была ли неделя релизной, подтверждает человек по календарю BLS/BEA",
            "4 различных периода считаются по зарегистрированным строкам (правило журнала, "
            f"docs/139); разрешённых периодов сейчас {len(periods_resolved)}",
            "сырые значения фактов не выводятся и не пишутся: только идентификаторы, даты и "
            "счётчики",
            "audit-лог производный (build/), журнал прогнозов остаётся единственным "
            "append-only источником и коммитится человеком",
        ],
    }


def render_process_markdown(document: dict) -> str:
    c6 = document["c6"]
    weeks = document["weeks"]
    alerts = document["alerts"]
    pair = document["pair"]
    lines = [
        f"# C6 — операционный отчёт ({document['as_of']}, неделя {document['week']})",
        "",
        f"Кандидат `{pair['candidate']}`, пара `{pair['target']}` / h={pair['horizon']}. "
        f"Журнал: `{document['source']['journal']}` "
        f"(SHA-256 `{(document['source']['journal_sha256'] or '-')[:12]}…`).",
        "",
        "**Статус 12/4: " + c6["status_line"] + f" — `{c6['status']}`. "
        "Loss/coverage не проверялись (нужна разрешённая выборка).**",
        "",
        "## 1. Критерии C6",
        "",
        "| Критерий | Текущее | Требуется | Выполнено |",
        "|---|---:|---:|---|",
        f"| Разрешённых прогнозов | {c6['n_resolved']} | {c6['min_forecasts']} | "
        f"{'да' if c6['counts_met'] else 'нет'} |",
        f"| Различных целевых периодов (зарегистрировано) | "
        f"{c6['distinct_target_periods']} | {c6['min_distinct_target_periods']} | "
        f"{'да' if c6['distinct_periods_met'] else 'нет'} |",
        f"| Различных периодов среди разрешённых | {c6['distinct_resolved_periods']} | — | — |",
        f"| Ждут публикации факта | {c6['pending_unresolved']} | — | — |",
        f"| Факт опубликован позже отчёта (не разрешены) | {c6['future_fact_blocked']} | — | — |",
        "",
        "## 2. Недели процесса",
        "",
        "| Неделя | Начало | Строк | Контролей | Проверка | Статус |",
        "|---|---|---:|---:|---|---|",
    ]
    for week in weeks["weeks"]:
        lines.append(f"| `{week['week']}` | {week['week_start']} | {week['candidate_rows']} | "
                     f"{week['control_rows']} | {'да' if week['checked'] else 'нет'} | "
                     f"{week['status']} |")
    lines += [
        "",
        f"Завершённых недель: {weeks['completed']}; со строками: {weeks['with_rows']}; "
        f"пропущено: {len(weeks['missed'])}; без WEEK_CHECK: {len(weeks['missed_checks'])}.",
        "",
        "## 3. Контрольная группа",
        "",
        f"Полная: **{'да' if document['control_group']['complete'] else 'нет'}**; групп: "
        f"{document['control_group']['groups']}; обязательные плечи: "
        f"{', '.join(document['control_group']['required_arms'])}.",
        "",
        "## 4. Неразрешённые прогнозы",
        "",
        f"- ждут факта: {document['unresolved']['pending']};",
        f"- поздние (>{document['rules']['unresolved_late_days']} дн.): "
        f"{len(document['unresolved']['late'])};",
        f"- просроченные (>{document['rules']['unresolved_alert_days']} дн.): "
        f"{len(document['unresolved']['overdue'])};",
        f"- винтажный архив доступен: {'да' if document['unresolved']['facts_available'] else 'нет'}.",
        "",
        "## 5. Алерты",
        "",
    ]
    if alerts:
        lines += [f"- **{item['severity']}** `{item['code']}` — {item['message']}"
                  for item in alerts]
    else:
        lines.append("- алертов нет")
    lines += ["", "## 6. Ограничения", ""]
    lines += [f"- {item}" for item in document["limitations"]]
    lines += ["", "Сгенерировано `tools/c6_calendar.py`; журнал не переписывается, сырые значения "
                  "фактов не выводятся.", ""]
    return "\n".join(lines)


def write_report(document: dict, out_path: Path) -> list[str]:
    out_path = Path(out_path)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    markdown = render_process_markdown(document)
    if out_path.suffix.lower() == ".json":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8", newline="\n")
        return [str(out_path)]
    if out_path.suffix.lower() == ".md":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8", newline="\n")
        return [str(out_path)]
    out_path.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_path / REPORT_JSON, out_path / REPORT_MD
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(markdown, encoding="utf-8", newline="\n")
    return [str(json_path), str(md_path)]


def weekly_run(*, journal: Path = DEFAULT_JOURNAL, audit: Path = DEFAULT_AUDIT, as_of: date,
               records=None, fact_lookup=None, candidate: str = DEFAULT_CANDIDATE,
               target: str = DEFAULT_TARGET, horizon: int = DEFAULT_HORIZON,
               require_control: bool = True, dry_run: bool = False) -> dict:
    """One weekly cycle: optional registration, resolution of published facts, report and audit."""
    as_of = _date(as_of, "as_of")
    registration = None
    if records is not None:
        registration = register_week(records, journal=journal, audit=audit, as_of=as_of,
                                     fact_lookup=fact_lookup, require_control=require_control,
                                     dry_run=dry_run)
    resolution = resolve_journal(journal=journal, audit=audit, as_of=as_of,
                                 fact_lookup=fact_lookup, persist=not dry_run)
    from registry import read_rows

    current_week = iso_week_label(as_of)
    week_rows = [row for row in read_rows(journal) if _row_week(row) == current_week]
    week_event = {"schema": AUDIT_SCHEMA, "event": "WEEK_CHECK", "at": as_of.isoformat(),
                  "week": current_week,
                  "candidate_rows": len([row for row in week_rows
                                         if row.get("target") == target
                                         and int(row.get("horizon", -1)) == int(horizon)
                                         and not row.get("control_arm")
                                         and row.get("candidate_id") not in CONTROL_ARMS]),
                  "control_rows": len([row for row in week_rows
                                       if row.get("target") == target
                                       and int(row.get("horizon", -1)) == int(horizon)
                                       and (row.get("control_arm")
                                            or row.get("candidate_id") in CONTROL_ARMS)]),
                  "registered": bool(registration and registration.get("appended"))}
    written = [] if dry_run else append_audit_events([week_event], audit)
    report = build_process_report(journal=journal, audit=audit, as_of=as_of,
                                  candidate=candidate, target=target, horizon=horizon,
                                  fact_lookup=fact_lookup)
    report["registration"] = registration
    report["resolution"] = {key: value for key, value in resolution.items() if key != "events"}
    report["audit_events_written"] = (registration or {}).get("audit_events_written", 0) \
        + resolution.get("written", 0) + len(written)
    report["dry_run"] = bool(dry_run)
    return report


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


def _run_operational(args, as_of: date) -> int:
    journal, audit = Path(args.journal), Path(args.audit)
    records = load_records(Path(args.records)) if args.records else None
    if args.register and records is None:
        raise C6CalendarError("--register needs --records FILE")
    if records is not None and not (args.register or args.weekly):
        raise C6CalendarError("--records is used with --register or --weekly")
    if args.weekly:
        document = weekly_run(journal=journal, audit=audit, as_of=as_of, records=records,
                              candidate=args.candidate, target=args.target, horizon=args.horizon,
                              dry_run=args.dry_run)
    else:
        actions = {}
        if args.register:
            actions["registration"] = register_week(records, journal=journal, audit=audit,
                                                    as_of=as_of, dry_run=args.dry_run)
        if args.resolve:
            actions["resolution"] = resolve_journal(journal=journal, audit=audit, as_of=as_of,
                                                    persist=not args.dry_run)
        document = build_process_report(journal=journal, audit=audit, as_of=as_of,
                                        candidate=args.candidate, target=args.target,
                                        horizon=args.horizon)
        document["actions"] = actions
    if args.report_out:
        document["written"] = write_report(document, Path(args.report_out))
    if args.json or args.report:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1))
    else:
        c6 = document["c6"]
        print(f"C6 ({document['as_of']}, {document['week']}): {c6['status_line']} — {c6['status']}")
        print(f"  недель завершено: {document['weeks']['completed']}; "
              f"пропущено строк: {len(document['weeks']['missed'])}; "
              f"без WEEK_CHECK: {len(document['weeks']['missed_checks'])}")
        print(f"  контрольная группа: "
              f"{'полная' if document['control_group']['complete'] else 'НЕПОЛНАЯ'}; "
              f"ждут факта: {document['unresolved']['pending']}")
        for item in document["alerts"]:
            print(f"  [{item['severity']}] {item['code']}: {item['message']}")
        for path in document.get("written", []):
            print(f"  записано: {path}")
    if args.strict and any(item["severity"] == "ALERT" for item in document["alerts"]):
        return 2
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="C6 calendar and operational process: release weeks, registration, resolution "
                    "and the 12/4 report (docs/139)")
    parser.add_argument("--status", default=str(DEFAULT_STATUS),
                        help="shadow status document (default build/shadow/shadow_status.json)")
    parser.add_argument("--date", default=None,
                        help="anchor/report date YYYY-MM-DD (default: status.date for the calendar, "
                             "today for the operational process)")
    parser.add_argument("--resolution-lag-days", dest="resolution_lag_days", type=int,
                        default=DEFAULT_RESOLUTION_LAG_DAYS,
                        help="assumed days between registration and resolution (default 30)")
    parser.add_argument("--json", action="store_true",
                        help="print the calendar or the process report as JSON")
    parser.add_argument("--out", default=None,
                        help="write the calendar: directory (json+md), .json or .md path")
    # operational process (docs/139 runbook)
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL),
                        help="forward journal (default registry/forecasts.jsonl)")
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT),
                        help="append-only audit log (default build/c6/c6_audit.jsonl)")
    parser.add_argument("--records", default=None,
                        help="JSON file with the weekly rows (forward_forecast.py --out)")
    parser.add_argument("--register", action="store_true",
                        help="register the weekly row and its control group from --records")
    parser.add_argument("--resolve", action="store_true",
                        help="resolve rows whose first publication is known, write the audit events")
    parser.add_argument("--weekly", action="store_true",
                        help="one weekly cycle: optional registration, resolution, report, WEEK_CHECK")
    parser.add_argument("--report", action="store_true", help="print the operational 12/4 report")
    parser.add_argument("--report-out", dest="report_out", default=None,
                        help="write the report: directory (json+md), .json or .md path")
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE,
                        help="candidate id of the reported pair (default %(default)s)")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="target of the reported pair (default %(default)s)")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                        help="horizon of the reported pair (default %(default)s)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="validate and report, do not append to the journal or the audit log")
    parser.add_argument("--strict", action="store_true",
                        help="exit 2 when any ALERT is raised (for CI gating)")
    args = parser.parse_args(argv)
    try:
        today = _date(args.date, "--date") if args.date else None
    except C6CalendarError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    operational = bool(args.register or args.resolve or args.weekly or args.report
                       or args.records)
    if operational:
        try:
            return _run_operational(args, today or date.today())
        except (C6CalendarError, OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
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
