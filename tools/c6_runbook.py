#!/usr/bin/env python3
"""Weekly C6 runbook: what may be done this week and what is still missing for C6 (docs/202).

The tool reads the two operational inputs and turns them into one weekly page:

* the append-only journal ``registry/forecasts.jsonl`` — the only permanent source of the
  registered rows (``candidate_id``, ``origin``, ``vintage_week``, ``registered_at``);
* the derived shadow status ``build/shadow/shadow_status.json`` — the projection assumption
  (``release_weeks_per_month``), the admission status and the status alerts.

It reports:

* the C6 counters **12/4** recomputed from the journal (a resolved forecast counts only when the
  first publication of the fact is known and is not later than the report date; a distinct target
  period counts on a registered row that is not rejected);
* the journal rows split into **allowed** (resolved and countable), **pending** (waiting for the
  fact), **blocked** (the fact exists but the first publication is after the report date — no
  look-ahead) and **rejected** (integrity problems: missing control group, look-ahead at
  registration, inconsistent vintage week, row dated after the report, superseded correction);
* the **nearest release windows** and the projected dates of the 12th forecast, the 4th distinct
  target period and the C6 gate — an *estimate* built with the same cadence model as
  ``tools/c6_calendar.py`` (average release cadence + resolution lag), not a release calendar;
* **what is missing** to C6 (forecasts, periods, control group, weekly check, loss/coverage);
* the **weekly checklist** of docs/139 with the command to run and the current step status;
* the **reasons why a row must not be registered this week** (``registration.cannot_register``).

The runbook never writes the journal, the audit log or the status document; ``--out`` writes only
the runbook itself (``c6_runbook.json`` / ``c6_runbook.md``) and nothing else.  The release week is
a *human* decision: without ``--confirm-release`` the tool always reports ``RELEASE_NOT_CONFIRMED``
and keeps ``can_register = false`` — it does not know the BLS/BEA calendar and does not guess.

Determinism: there is no clock read and no timestamp in the document; ``as_of`` defaults to
``status.date`` (not to ``date.today()``), the same journal/status/``--date``/flags always produce
byte-identical JSON and Markdown.

Fail-closed: a missing/unreadable journal or status, an invalid journal (registry schema or
append-only rules), a row registered after the report date, a fact without a first-publication date,
a bad origin or a non-integer horizon raise :class:`C6RunbookError` (a subclass of
``c6_calendar.C6CalendarError``) instead of producing an approximate runbook.

CLI:
    python tools/c6_runbook.py                              # weekly page for status.date
    python tools/c6_runbook.py --date 2026-11-20            # explicit report date
    python tools/c6_runbook.py --date 2026-11-23 --confirm-release   # human confirmed the release
    python tools/c6_runbook.py --json                       # machine-readable document
    python tools/c6_runbook.py --out build/c6               # writes c6_runbook.json + c6_runbook.md
    python tools/c6_runbook.py --check                      # input/consistency validation, exit 1 on problems
    python tools/c6_runbook.py --check --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:  # direct script execution
    sys.path.insert(0, str(ROOT / "tools"))

import c6_calendar as cal  # noqa: E402

SCHEMA = "AMFE-C6-RUNBOOK/1.0.0"
CHECK_SCHEMA = "AMFE-C6-RUNBOOK-CHECK/1.0.0"
RUNBOOK_JSON = "c6_runbook.json"
RUNBOOK_MD = "c6_runbook.md"
MAX_RELEASE_WINDOWS = 6          # windows shown in the page
WINDOW_PLAN_LIMIT = 200          # windows generated for the projection (same cap as the calendar)
CADENCE_WARN_DAYS = 25           # docs/119: no new candidate row for > 25 days -> WARN
CADENCE_ALERT_DAYS = 35          # docs/119: no new candidate row for > 35 days -> ALERT
PROJECTION_SHIFT_WARN_DAYS = cal.PROJECTION_SHIFT_WARN_DAYS

#: codes that make a journal row unusable for C6 (the row is shown, never counted)
REJECT_CODES = (
    "BAD_ORIGIN",
    "MISSING_VINTAGE_WEEK",
    "BAD_VINTAGE_WEEK",
    "MISSING_REGISTERED_AT",
    "VINTAGE_WEEK_MISMATCH",
    "ROW_AFTER_REPORT",
    "CONTROL_GROUP_MISSING",
    "PUBLISHED_BEFORE_REGISTRATION",
    "SUPERSEDED_BY_CORRECTION",
)


class C6RunbookError(cal.C6CalendarError):
    """Missing/broken input or a journal row that cannot be judged without guessing."""


# --------------------------------------------------------------------------------------- helpers
def _parse_date(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise C6RunbookError(f"{name} is not an ISO date: {value!r}") from exc


def _int(value, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise C6RunbookError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise C6RunbookError(f"{name} must be >= {minimum}, got {value}")
    return value


def _horizon(row: dict):
    value = row.get("horizon")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _registered_day(row: dict) -> date | None:
    value = row.get("registered_at")
    if value is None or str(value).strip() == "":
        return None
    return _parse_date(value, "registered_at")


def _row_week(row: dict) -> str | None:
    label = row.get("vintage_week")
    if label:
        return str(label)
    day = _registered_day(row)
    return cal.iso_week_label(day) if day else None


def _target_kind(target: str) -> str:
    return "event" if target == "T3_RECESSION" else "quantiles"


def _row_ref(origin, week) -> str:
    return f"{origin}/{week or 'no-week'}"


def _read_journal(journal: Path) -> list[dict]:
    """Validate and read the append-only journal; a missing or invalid file is fail-closed."""
    from registry import RegistryError, validate_journal

    path = Path(journal)
    if not path.is_file():
        raise C6RunbookError(f"journal not found: {path}")
    try:
        rows, problems = validate_journal(path)
    except RegistryError as exc:
        raise C6RunbookError(f"journal is not readable: {exc}") from exc
    if problems:
        raise C6RunbookError("journal is not valid: " + "; ".join(problems))
    return rows


def _memo_facts(fact_lookup):
    base = fact_lookup or cal.default_fact_lookup
    cache: dict[tuple, dict] = {}

    def lookup(target, origin, horizon):
        key = (target, str(origin), int(horizon))
        if key not in cache:
            try:
                fact = base(target, origin, int(horizon)) or {}
            except Exception as exc:
                raise C6RunbookError(
                    f"cannot read the first-publication actual for {target} {origin} "
                    f"h={horizon}: {exc!r}") from exc
            cache[key] = {"primary": fact.get("primary"),
                          "primary_vintage": fact.get("primary_vintage"),
                          "available": bool(fact.get("available", True))}
        return cache[key]

    return lookup


def _read_audit(audit: Path) -> list[dict]:
    try:
        return cal.read_audit(audit)
    except cal.C6CalendarError as exc:
        raise C6RunbookError(str(exc)) from exc


# --------------------------------------------------------------------------------------- journal
def _pair_arms(rows: list[dict], target: str, horizon: int) -> dict:
    """Arms and candidate rows per (origin, vintage_week) group of the reported pair."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        if row.get("target") != target or _horizon(row) != horizon:
            continue
        key = (row.get("origin"), _row_week(row))
        group = groups.setdefault(key, {"candidate_rows": 0, "arms": set()})
        candidate_id = row.get("candidate_id")
        if row.get("control_arm") or candidate_id in cal.CONTROL_ARMS:
            group["arms"].add(candidate_id)
        else:
            group["candidate_rows"] += 1
    return groups


def classify_rows(*, rows: list[dict], candidate: str, target: str, horizon: int,
                  as_of: date, fact_lookup) -> dict:
    """Split the candidate rows of the pair into allowed / pending / blocked / rejected."""
    lookup = _memo_facts(fact_lookup)
    groups = _pair_arms(rows, target, horizon)
    required = list(cal.COMPARATOR_ARMS[_target_kind(target)])
    latest: dict[tuple, int] = {}
    for index, row in enumerate(rows):
        latest[(row.get("candidate_id"), row.get("target"), row.get("horizon"),
                row.get("origin"), row.get("vintage_week"))] = index

    entries: list[dict] = []
    facts_available = True
    for index, row in enumerate(rows):
        if row.get("candidate_id") != candidate or row.get("target") != target \
                or _horizon(row) != horizon:
            continue
        codes: list[str] = []
        reasons: list[str] = []
        first_publication = None

        def flag(code: str, message: str) -> None:
            if code not in codes:
                codes.append(code)
                reasons.append(message)

        identity = (row.get("candidate_id"), row.get("target"), row.get("horizon"),
                    row.get("origin"), row.get("vintage_week"))
        ref = _row_ref(row.get("origin"), _row_week(row))
        if latest.get(identity) != index:
            flag("SUPERSEDED_BY_CORRECTION",
                 f"{ref}: строка заменена более поздней коррекцией и не считается")

        period = None
        origin = row.get("origin")
        try:
            period = cal.protocol_period(target, origin, horizon)
        except cal.C6CalendarError:
            flag("BAD_ORIGIN", f"{ref}: origin {origin!r} не является месяцем YYYY-MM")

        week = row.get("vintage_week")
        week_start = None
        if not week:
            flag("MISSING_VINTAGE_WEEK",
                 f"{ref}: у строки нет vintage_week — point-in-time неделю доказать нельзя")
        else:
            try:
                week_start = cal.week_start_from_label(str(week))
            except cal.C6CalendarError:
                flag("BAD_VINTAGE_WEEK", f"{ref}: vintage_week {week!r} не ISO-неделя YYYY-Www")

        registered = _registered_day(row)
        if registered is None:
            flag("MISSING_REGISTERED_AT", f"{ref}: нет registered_at — PIT-порядок не проверить")
        else:
            if registered > as_of:
                flag("ROW_AFTER_REPORT",
                     f"{ref}: registered_at {registered} позже отчётной даты {as_of}")
            if week and week_start is not None \
                    and cal.iso_week_label(registered) != str(week):
                flag("VINTAGE_WEEK_MISMATCH",
                     f"{ref}: vintage_week {week} не совпадает с неделей registered_at "
                     f"({cal.iso_week_label(registered)})")

        group = groups.get((origin, _row_week(row)), {"candidate_rows": 0, "arms": set()})
        arms = sorted(group["arms"])
        missing_arms = [arm for arm in required if arm not in group["arms"]]
        if missing_arms:
            flag("CONTROL_GROUP_MISSING",
                 f"{ref}: нет контрольных плеч {missing_arms}; без них строка не считается")

        fact = {"primary": None, "primary_vintage": None, "available": True}
        published = None
        if period is not None:
            fact = lookup(target, origin, horizon)
            facts_available = facts_available and bool(fact.get("available", True))
            if fact.get("primary") is not None:
                if not fact.get("primary_vintage"):
                    raise C6RunbookError(
                        f"{ref}: the first-publication date is missing; cannot prove no look-ahead")
                published = _parse_date(fact["primary_vintage"], "primary_vintage")
                first_publication = published.isoformat()
                if registered is not None and published <= registered:
                    flag("PUBLISHED_BEFORE_REGISTRATION",
                         f"{ref}: первая публикация {published} не позже регистрации "
                         f"{registered} — это look-ahead")

        if published is not None and published > as_of:
            flag("FUTURE_FACT_BLOCKED",
                 f"{ref}: первая публикация {published} позже отчётной даты {as_of} — "
                 "строка не разрешается (no look-ahead)")

        if any(code in REJECT_CODES for code in codes):
            state = "REJECTED"
        elif published is not None:
            state = "RESOLVED" if published <= as_of else "BLOCKED"
        else:
            state = "PENDING"

        entry = {"ref": ref, "origin": origin, "vintage_week": _row_week(row),
                 "registered_at": row.get("registered_at"),
                 "target_period": period["label"] if period else None,
                 "period_end": period["end"].isoformat() if period else None,
                 "first_publication": first_publication,
                 "state": state, "codes": codes, "reasons": reasons,
                 "control_arms": arms, "missing_arms": missing_arms,
                 "facts_available": bool(fact.get("available", True)),
                 "counts_toward_c6": state == "RESOLVED"}
        entries.append(entry)

    buckets = {name: [entry["ref"] for entry in entries if entry["state"] == name]
               for name in ("RESOLVED", "PENDING", "BLOCKED", "REJECTED")}
    resolved = [entry for entry in entries if entry["state"] == "RESOLVED"]
    effective = [entry for entry in entries if entry["state"] != "REJECTED"]
    periods_resolved = {entry["target_period"] for entry in resolved if entry["target_period"]}
    periods_registered = {entry["target_period"] for entry in effective if entry["target_period"]}
    periods_all = {entry["target_period"] for entry in entries if entry["target_period"]}

    control_groups = {}
    for (origin, week), group in groups.items():
        if not group["candidate_rows"]:
            continue
        missing = [arm for arm in required if arm not in group["arms"]]
        control_groups[(origin, week)] = {
            "origin": origin, "vintage_week": week, "candidate_rows": group["candidate_rows"],
            "arms": sorted(group["arms"]), "missing_arms": missing, "complete": not missing}
    incomplete = [value for value in control_groups.values() if not value["complete"]]

    return {"entries": entries, "buckets": buckets, "facts_available": facts_available,
            "n_resolved": len(resolved), "periods_resolved": sorted(periods_resolved),
            "periods_registered": sorted(periods_registered), "periods_all": sorted(periods_all),
            "control_groups": [control_groups[key] for key in sorted(
                control_groups, key=lambda item: (str(item[0]), str(item[1])))],
            "incomplete_groups": incomplete}


# -------------------------------------------------------------------------------- release windows
def _window_plan(*, anchor: date, target: str, horizon: int, release_weeks: float,
                 lag_days: int, count: int) -> list[dict]:
    """Projected release weeks after ``anchor`` (same cadence model as tools/c6_calendar.py)."""
    cadence = 30.0 / float(release_weeks)
    plan = []
    for step in range(1, count + 1):
        start = cal.next_release_week(anchor, step, cadence)
        origin = f"{start.year:04d}-{start.month:02d}"
        period = cal.protocol_period(target, origin, horizon)
        plan.append({"sequence": step, "week": cal.iso_week_label(start),
                     "week_start": start, "week_end": start + timedelta(days=6),
                     "estimated_origin": origin, "estimated_target_period": period["label"],
                     "resolves_by": start + timedelta(days=lag_days)})
    return plan


def _projection(*, as_of: date, n_resolved: int, distinct_periods: int,
                registered_periods: list[str], pending_dates: list[date],
                blocked_dates: list[date], plan: list[dict], status_doc: dict) -> dict:
    """Estimate the 12th forecast, the 4th period and the C6 gate from the journal counts."""
    remaining = max(0, cal.MIN_FORECASTS - n_resolved)
    events = list(blocked_dates) + list(pending_dates)
    events += [item["resolves_by"] for item in plan]
    events.sort()
    if remaining == 0:
        date_12th = as_of
    else:
        date_12th = events[remaining - 1] if len(events) >= remaining else None

    periods_needed = max(0, cal.MIN_DISTINCT_PERIODS - distinct_periods)
    date_periods = None
    if periods_needed == 0:
        date_periods = as_of
    else:
        seen = set(registered_periods)
        found = 0
        for item in plan:
            if item["estimated_target_period"] in seen:
                continue
            seen.add(item["estimated_target_period"])
            found += 1
            if found >= periods_needed:
                date_periods = item["week_start"]
                break

    criteria = [value for value in (date_12th, date_periods) if value is not None]
    if remaining == 0 and periods_needed == 0:
        gate = as_of
    else:
        gate = max(criteria) if criteria else None
    status_gate = (status_doc.get("projection") or {}).get("estimated_gate_date")
    shift = None
    if gate is not None and status_gate:
        shift = (gate - _parse_date(status_gate, "projection.estimated_gate_date")).days
    return {"date_12th_resolved": date_12th.isoformat() if date_12th else None,
            "date_4th_period": date_periods.isoformat() if date_periods else None,
            "gate_date": gate.isoformat() if gate else None,
            "months_to_gate": round((gate - as_of).days / 30.0, 1) if gate else None,
            "weeks_to_12th": max(0, int(round((date_12th - as_of).days / 7.0)))
            if date_12th else None,
            "status_estimated_gate_date": status_gate,
            "shift_vs_status_days": shift,
            "estimate": True,
            "note": ("дата — оценка по средней частоте релизных недель и лагу разрешения; "
                     "реальные релизные недели подтверждает человек (BLS/BEA)")}


# -------------------------------------------------------------------------------------- checklist
CHECKLIST = (
    (1, "OPEN_WEEK", "Открыть ISO-неделю и отчётную дату"),
    (2, "CONFIRM_RELEASE", "Подтвердить по календарю BLS/BEA, был ли первый релиз данных по цели"),
    (3, "NO_RELEASE_NO_ROW", "Если релизной недели не было — строку не добавлять: фиксируется "
                             "только WEEK_CHECK"),
    (4, "BUILD_BATCH", "Собрать батч кандидата и контрольных плеч (dry-run, без записи)"),
    (5, "REGISTER_WEEK", "Зарегистрировать недельный цикл: строки + WEEK_CHECK + отчёт"),
    (6, "VERIFY_REPORT", "Проверить отчёт 12/4, контрольную группу и алерты"),
    (7, "VALIDATE_JOURNAL", "Проверить журнал прогнозов"),
    (8, "MONITOR_TRACK", "Пересобрать теневой трек и календарь"),
    (9, "RECORD_DECISION", "Записать решение D-… только при изменении статуса"),
)


def _checklist(*, as_of: date, week: str, candidate: str, target: str, horizon: int,
               week_rows: list[str], confirmed: bool, blocked: bool,
               journal_valid: bool, week_checked: bool, audit_present: bool) -> list[dict]:
    register_command = (f"python tools/c6_calendar.py --weekly --records build/forward_week.json "
                        f"--date {as_of.isoformat()} --audit build/c6/c6_audit.jsonl "
                        f"--report-out build/c6")
    batch_command = (f"python tools/forward_forecast.py --origin {as_of.year:04d}-{as_of.month:02d} "
                     f"--candidates {candidate} --targets {target}:{horizon} --mode latest "
                     f"--vintage-week {week} --dry-run --out build/forward_week.json")
    skip_command = (f"python tools/c6_calendar.py --weekly --date {as_of.isoformat()} "
                    f"--audit build/c6/c6_audit.jsonl --report-out build/c6")
    steps = []
    for number, code, action in CHECKLIST:
        entry = {"step": number, "code": code, "action": action, "command": None,
                 "status": "TODO", "note": None}
        if code == "OPEN_WEEK":
            entry["status"] = "DONE"
            entry["note"] = f"{week} ({as_of.isoformat()})"
        elif code == "CONFIRM_RELEASE":
            entry["status"] = "DONE" if confirmed else "NEEDS_HUMAN"
            entry["note"] = ("релизная неделя подтверждена человеком (--confirm-release)"
                             if confirmed else
                             "инструмент не знает календарь BLS/BEA: неделю подтверждает человек")
        elif code == "NO_RELEASE_NO_ROW":
            entry["status"] = "DONE"
            entry["command"] = skip_command
            entry["note"] = "применяется, если первого релиза по цели на неделе не было"
        elif code in ("BUILD_BATCH", "REGISTER_WEEK"):
            if week_rows:
                entry["status"] = "DONE"
                entry["note"] = f"строки недели уже в журнале: {', '.join(week_rows)}"
            elif blocked:
                entry["status"] = "BLOCKED"
            else:
                entry["status"] = "TODO"
            entry["command"] = batch_command if code == "BUILD_BATCH" else register_command
        elif code == "VERIFY_REPORT":
            entry["command"] = (f"python tools/c6_calendar.py --report --date {as_of.isoformat()}")
            entry["note"] = "ожидается: нет ALERT, контрольная группа полная"
        elif code == "VALIDATE_JOURNAL":
            entry["status"] = "DONE" if journal_valid else "BLOCKED"
            entry["command"] = "python tools/registry.py --validate"
            entry["note"] = "valid: true"
        elif code == "MONITOR_TRACK":
            entry["command"] = (f"python tools/shadow_track.py --with-calendar "
                                f"--date {as_of.isoformat()}")
            entry["note"] = "build/shadow/<дата>.md|json"
        elif code == "RECORD_DECISION":
            entry["status"] = "TODO"
            entry["note"] = "запись не нужна, если статус трека/кандидата не изменился"
        if code == "REGISTER_WEEK" and audit_present and week_checked:
            entry["note"] = ((entry["note"] + "; ") if entry["note"] else "") \
                + f"WEEK_CHECK {week} уже есть в аудите"
        steps.append(entry)
    return steps


# -------------------------------------------------------------------------------------- runbook
def build_runbook(*, journal: Path = cal.DEFAULT_JOURNAL, status: Path = cal.DEFAULT_STATUS,
                  audit: Path = cal.DEFAULT_AUDIT, as_of=None,
                  candidate: str = cal.DEFAULT_CANDIDATE, target: str = cal.DEFAULT_TARGET,
                  horizon: int = cal.DEFAULT_HORIZON, fact_lookup=None,
                  release_confirmed: bool = False,
                  resolution_lag_days: int = cal.DEFAULT_RESOLUTION_LAG_DAYS) -> dict:
    """Build the weekly C6 runbook from the journal and the derived shadow status."""
    rows = _read_journal(journal)
    status_doc = cal.load_status(status)
    audit_events = _read_audit(audit)
    audit_present = Path(audit).is_file()
    if as_of is None:
        as_of = _parse_date(status_doc.get("date"), "status.date")
        as_of_source = "status.date"
    else:
        as_of = _parse_date(as_of, "as_of")
        as_of_source = "--date"
    horizon = _int(horizon, "horizon", minimum=0)
    lag = _int(resolution_lag_days, "resolution_lag_days", minimum=0)
    week = cal.iso_week_label(as_of)
    week_start = cal.monday_of(as_of)

    later_rows = [row for row in rows if (_registered_day(row) or as_of) > as_of]
    if later_rows:
        first = later_rows[0]
        raise C6RunbookError(
            f"journal has {len(later_rows)} row(s) registered after the report date {as_of} "
            f"(e.g. {first.get('candidate_id')} {_row_ref(first.get('origin'), _row_week(first))}); "
            "pass --date on/after the last registration or refresh the shadow status")

    classified = classify_rows(rows=rows, candidate=candidate, target=target, horizon=horizon,
                               as_of=as_of, fact_lookup=fact_lookup)
    entries = classified["entries"]
    buckets = classified["buckets"]
    n_resolved = classified["n_resolved"]
    periods_registered = classified["periods_registered"]
    distinct = len(periods_registered)
    counts_met = n_resolved >= cal.MIN_FORECASTS
    periods_met = distinct >= cal.MIN_DISTINCT_PERIODS
    counts_and_periods_met = counts_met and periods_met
    if counts_and_periods_met:
        state = "COUNTS_MET"
    elif n_resolved == 0:
        state = "STARTING"
    else:
        state = "IN_PROGRESS"
    remaining_forecasts = max(0, cal.MIN_FORECASTS - n_resolved)
    remaining_periods = max(0, cal.MIN_DISTINCT_PERIODS - distinct)
    pending_count = len(buckets["PENDING"])
    registrations_needed = max(0, remaining_forecasts - pending_count - len(buckets["BLOCKED"]))

    # -- release windows and projection ------------------------------------------------------
    # the cadence model is anchored at the last registration (the track's own cadence), not at
    # the report date: the release weeks do not move because the page is opened later
    last_registered = max((_registered_day(entry) for entry in entries
                           if _registered_day(entry)), default=None)
    anchor = last_registered or as_of
    release_weeks = float((status_doc.get("projection") or {}).get("release_weeks_per_month", 2.5))
    plan = [item for item in _window_plan(anchor=anchor, target=target, horizon=horizon,
                                          release_weeks=release_weeks, lag_days=lag,
                                          count=WINDOW_PLAN_LIMIT + 100)
            if item["week_end"] >= as_of][:WINDOW_PLAN_LIMIT]
    seen_periods = set(periods_registered)
    for ordinal, item in enumerate(plan, start=1):
        item["sequence"] = ordinal
        item["adds_distinct_period"] = item["estimated_target_period"] not in seen_periods
        if item["adds_distinct_period"]:
            seen_periods.add(item["estimated_target_period"])
        item["needed"] = ordinal <= registrations_needed
    windows = [{"sequence": item["sequence"], "week": item["week"],
                "week_start": item["week_start"].isoformat(),
                "week_end": item["week_end"].isoformat(),
                "estimated_origin": item["estimated_origin"],
                "estimated_target_period": item["estimated_target_period"],
                "resolves_by": item["resolves_by"].isoformat(),
                "adds_distinct_period": item["adds_distinct_period"],
                "needed": item["needed"],
                "purpose": "REGISTRATION_WINDOW",
                "row_status": "PROJECTED_NOT_REGISTERED"}
               for item in plan[:MAX_RELEASE_WINDOWS]]
    current_week_is_window = any(item["week_start"] <= as_of <= item["week_end"] for item in plan)
    pending_dates = [as_of + timedelta(days=lag)] * pending_count
    blocked_dates = [_parse_date(entry["first_publication"], "first_publication")
                     for entry in entries
                     if entry["state"] == "BLOCKED" and entry["first_publication"]]
    projection = _projection(as_of=as_of, n_resolved=n_resolved, distinct_periods=distinct,
                             registered_periods=periods_registered,
                             pending_dates=pending_dates, blocked_dates=blocked_dates,
                             plan=plan, status_doc=status_doc)
    projection["release_weeks_per_month"] = release_weeks
    projection["cadence_days"] = round(30.0 / release_weeks, 3)
    projection["resolution_lag_days"] = lag
    days_to_next = ((date.fromisoformat(windows[0]["week_start"]) - week_start).days
                    if windows else None)

    # -- registration gate -------------------------------------------------------------------
    week_rows = [entry["ref"] for entry in entries
                 if entry["vintage_week"] == week and entry["state"] != "REJECTED"]
    rejected_week_rows = [entry["ref"] for entry in entries
                          if entry["vintage_week"] == week and entry["state"] == "REJECTED"
                          and "SUPERSEDED_BY_CORRECTION" not in entry["codes"]]
    prospective_origin = f"{as_of.year:04d}-{as_of.month:02d}"
    prospective_period = cal.protocol_period(target, prospective_origin, horizon)
    prospective_fact = _memo_facts(fact_lookup)(target, prospective_origin, horizon)
    blockers: list[dict] = []
    gate_warnings: list[dict] = []

    def block(code: str, message: str) -> None:
        blockers.append({"code": code, "severity": "BLOCK", "message": message})

    def caution(code: str, message: str) -> None:
        gate_warnings.append({"code": code, "severity": "WARN", "message": message})

    if not release_confirmed:
        block("RELEASE_NOT_CONFIRMED",
              "релизная неделя не подтверждена человеком: инструмент не знает календарь BLS/BEA; "
              "--confirm-release передаётся только после проверки первого релиза по цели")
    if week_rows:
        block("WEEK_ALREADY_REGISTERED",
              f"в неделе {week} уже есть строка кандидата по паре ({', '.join(week_rows)}): "
              "вторая строка той же недели не регистрируется")
    if rejected_week_rows:
        block("WEEK_ROW_REJECTED",
              f"в неделе {week} есть отклонённая строка кандидата "
              f"({', '.join(rejected_week_rows)}): сначала устранить нарушение целостности "
              "(контрольная группа / vintage_week), новую строку не добавлять")
    if prospective_fact.get("primary") is not None:
        block("FACT_ALREADY_PUBLISHED",
              f"первая публикация факта за {prospective_period['label']} уже есть "
              f"({prospective_fact.get('primary_vintage')}): регистрация была бы look-ahead")
    for alert in status_doc.get("alerts") or []:
        if str(alert.get("severity")).upper() == "ALERT":
            block(f"STATUS_{alert.get('code')}",
                  f"статус трека в ALERT ({alert.get('code')}): {alert.get('message')}")
    if not classified["facts_available"]:
        caution("FACTS_UNAVAILABLE",
                "винтажный архив недоступен: статус публикации факта не подтверждён")
    status_date = _parse_date(status_doc.get("date"), "status.date")
    if status_date < as_of:
        caution("STATUS_STALE",
                f"статус от {status_date} старше отчётной даты {as_of}: счётчики статуса "
                "могут отставать от журнала (runbook считает по журналу)")
    required_arms = cal.COMPARATOR_ARMS[_target_kind(target)]
    booked = set()
    for group in classified["control_groups"]:
        if group["vintage_week"] == week:
            booked.update(group["arms"])
    missing_for_week = [arm for arm in required_arms if arm not in booked]
    if missing_for_week:
        caution("CONTROL_ARMS_NOT_BOOKED",
                f"для недели {week} контрольные плечи {missing_for_week} ещё не в журнале: "
                "батч должен включать их вместе с кандидатом")
    if release_confirmed and not current_week_is_window:
        caution("RELEASE_OUTSIDE_CALENDAR",
                f"подтверждённый релиз не совпадает с оценочным окном "
                f"({windows[0]['week'] if windows else '—'}): это допустимо, но источник "
                "подтверждения (BLS/BEA) нужно записать")
    can_register = not blockers

    # -- missing / gaps ----------------------------------------------------------------------
    missing: list[dict] = []
    if remaining_forecasts:
        missing.append({"code": "FORECASTS", "have": n_resolved, "need": cal.MIN_FORECASTS,
                        "remaining": remaining_forecasts,
                        "note": f"разрешённых прогнозов не хватает {remaining_forecasts}; "
                                "loss/coverage считаются по разрешённой выборке"})
    if remaining_periods:
        missing.append({"code": "PERIODS", "have": distinct,
                        "need": cal.MIN_DISTINCT_PERIODS, "remaining": remaining_periods,
                        "note": f"нужно ещё {remaining_periods} различных целевых периода"})
    if classified["incomplete_groups"]:
        missing.append({"code": "CONTROL_GROUP",
                        "groups": len(classified["incomplete_groups"]),
                        "details": classified["incomplete_groups"],
                        "note": "неполная контрольная группа: C6-потери сравнивать не с чем"})
    checked_weeks = {event["week"] for event in audit_events
                     if event.get("event") == "WEEK_CHECK"}
    week_checked = week in checked_weeks
    if not week_checked:
        missing.append({"code": "WEEK_CHECK", "week": week,
                        "note": "недельный цикл не зафиксирован в аудите (WEEK_CHECK); "
                                "прошлые недели задним числом не выдумываются"})
    missing.append({"code": "LOSS_COVERAGE", "checked": False,
                    "note": "loss/coverage считает tools/registry_resolve.py по разрешённой "
                            "выборке; runbook их не пересчитывает и не выдаёт за пройденные"})

    # -- alerts ------------------------------------------------------------------------------
    alerts: list[dict] = []

    def alert(code: str, severity: str, message: str) -> None:
        alerts.append({"code": code, "severity": severity, "message": message})

    if not entries:
        alert("NO_ROWS", "WARN", f"в журнале нет строк кандидата {candidate} для {target} "
                                 f"h={horizon}")
    if not classified["facts_available"]:
        alert("FACTS_UNAVAILABLE", "WARN",
              "винтажный архив отсутствует: разрешение и просрочка не проверяются")
    status_counts = status_doc.get("c6") or {}
    if classified["facts_available"] and status_counts.get("n_resolved") not in (None, n_resolved):
        alert("STATUS_COUNTS_MISMATCH", "WARN",
              f"статус сообщает {status_counts.get('n_resolved')} разрешённых, журнал даёт "
              f"{n_resolved}: статус устарел, runbook считает по журналу")
    if distinct != status_counts.get("distinct_target_periods") \
            and status_counts.get("distinct_target_periods") is not None:
        alert("STATUS_PERIODS_MISMATCH", "WARN",
              f"статус сообщает {status_counts.get('distinct_target_periods')} периодов, "
              f"журнал даёт {distinct}")
    if status_date < as_of:
        alert("STATUS_STALE", "INFO",
              f"статус от {status_date} старше отчётной даты {as_of}")
    days_since_last = (as_of - last_registered).days if last_registered else None
    if days_since_last is not None and days_since_last > CADENCE_ALERT_DAYS:
        alert("SUSPEND_CADENCE", "ALERT",
              f"нет новой строки кандидата {days_since_last} дн. (>{CADENCE_ALERT_DAYS}): "
              "восстановить cadence в ближайшую релизную неделю")
    elif days_since_last is not None and days_since_last > CADENCE_WARN_DAYS:
        alert("WARN_CADENCE", "WARN",
              f"нет новой строки кандидата {days_since_last} дн. (>{CADENCE_WARN_DAYS})")
    if classified["incomplete_groups"]:
        alert("CONTROL_GROUP_MISSING", "ALERT",
              f"неполная контрольная группа у {len(classified['incomplete_groups'])} групп(ы): "
              + "; ".join(f"{item['origin']}/{item['vintage_week']}: {item['missing_arms']}"
                          for item in classified["incomplete_groups"][:4]))
    late, overdue = [], []
    for entry in entries:
        if entry["state"] not in ("PENDING", "BLOCKED") or not entry["period_end"]:
            continue
        period_end = _parse_date(entry["period_end"], "period_end")
        age = (as_of - period_end).days
        record = {"ref": entry["ref"], "period": entry["target_period"],
                  "period_end": entry["period_end"], "days_since_period_end": max(0, age)}
        if age > cal.UNRESOLVED_ALERT_DAYS:
            overdue.append(record)
        elif age > cal.UNRESOLVED_LATE_DAYS:
            late.append(record)
    if overdue:
        alert("UNRESOLVED_OVERDUE", "ALERT",
              f"{len(overdue)} строк(и) не разрешены позже {cal.UNRESOLVED_ALERT_DAYS} дн. после "
              f"конца целевого периода (самый старый: {overdue[0]['period']})")
    if late:
        alert("UNRESOLVED_LATE", "WARN",
              f"{len(late)} строк(и) не разрешены через {cal.UNRESOLVED_LATE_DAYS}+ дн. после "
              "конца целевого периода: проверить публикацию факта")
    if buckets["BLOCKED"]:
        alert("FUTURE_FACT_BLOCKED", "INFO",
              f"{len(buckets['BLOCKED'])} строк(и) имеют первую публикацию позже отчётной даты "
              "и не разрешены (no look-ahead)")
    if pending_count:
        alert("PENDING_RESOLUTION", "INFO",
              f"{pending_count} строк(и) ждут публикации факта — это ожидаемо, а не сбой")
    if not audit_present:
        alert("NO_AUDIT", "INFO", "аудит-лог процесса отсутствует: история недель не проверяется")
    elif not week_checked:
        alert("NO_WEEK_CHECK", "INFO",
              f"за неделю {week} нет WEEK_CHECK: еженедельный цикл ещё не зафиксирован")
    if counts_and_periods_met:
        alert("COUNTS_MET", "INFO",
              "12/4 по количеству и периодам выполнены; loss/coverage проверяются отдельно")
    projection_shift = projection.get("shift_vs_status_days")
    if projection_shift is not None and abs(projection_shift) > PROJECTION_SHIFT_WARN_DAYS:
        explained = projection_shift < 0 and pending_count > 0
        alert("PROJECTION_SHIFT", "INFO" if explained else "WARN",
              f"runbook даёт дату гейта {projection['gate_date']} — на "
              f"{abs(projection_shift)} дней {'раньше' if projection_shift < 0 else 'позже'} "
              f"ориентира статуса {projection['status_estimated_gate_date']}"
              + ("; разница объяснима: pending-строки уже зарегистрированы" if explained else ""))
    for item in status_doc.get("alerts") or []:
        alert(f"STATUS_{item.get('code')}", str(item.get("severity") or "INFO").upper(),
              f"статус трека: {item.get('code')} — {item.get('message')}")

    steps = _checklist(as_of=as_of, week=week, candidate=candidate, target=target,
                       horizon=horizon, week_rows=week_rows, confirmed=release_confirmed,
                       blocked=not release_confirmed or bool(blockers),
                       journal_valid=True, week_checked=week_checked,
                       audit_present=audit_present)
    status_line = (f"{n_resolved}/{cal.MIN_FORECASTS} прогнозов, "
                   f"{distinct}/{cal.MIN_DISTINCT_PERIODS} периодов")

    return {
        "schema": SCHEMA,
        "as_of": as_of.isoformat(),
        "as_of_source": as_of_source,
        "week": {"label": week, "start": week_start.isoformat(),
                 "end": (week_start + timedelta(days=6)).isoformat()},
        "pair": {"candidate": candidate, "target": target, "horizon": horizon},
        "source": {"journal": _repo_path(journal),
                   "journal_sha256": cal.sha256_file(journal),
                   "journal_rows": len(rows),
                   "status": _repo_path(status),
                   "status_sha256": cal.sha256_file(status),
                   "status_date": status_date.isoformat(),
                   "status_state": status_doc.get("status"),
                   "audit": _repo_path(audit),
                   "audit_events": len(audit_events),
                   "raw_values": False},
        "c6": {"n_resolved": n_resolved, "min_forecasts": cal.MIN_FORECASTS,
               "forecasts_line": f"{n_resolved}/{cal.MIN_FORECASTS}",
               "counts_met": counts_met,
               "distinct_target_periods": distinct,
               "distinct_target_periods_all": len(classified["periods_all"]),
               "min_distinct_periods": cal.MIN_DISTINCT_PERIODS,
               "periods_line": f"{distinct}/{cal.MIN_DISTINCT_PERIODS}",
               "distinct_periods_met": periods_met,
               "distinct_resolved_periods": len(classified["periods_resolved"]),
               "counts_and_periods_met": counts_and_periods_met,
               "remaining_forecasts": remaining_forecasts,
               "remaining_periods": remaining_periods,
               "registrations_needed": registrations_needed,
               "pending": pending_count, "blocked": len(buckets["BLOCKED"]),
               "rejected": len(buckets["REJECTED"]),
               "status": state, "status_line": status_line,
               "gate": ("COUNTS_MET_LOSS_PENDING" if counts_and_periods_met else "NOT_MET"),
               "loss_coverage_checked": False},
        "rows": {"total": len(entries),
                 "counts": {"allowed": len(buckets["RESOLVED"]),
                            "pending": pending_count,
                            "blocked": len(buckets["BLOCKED"]),
                            "rejected": len(buckets["REJECTED"])},
                 "allowed": buckets["RESOLVED"], "pending": buckets["PENDING"],
                 "blocked": buckets["BLOCKED"], "rejected": buckets["REJECTED"],
                 "entries": entries},
        "control_group": {"complete": not classified["incomplete_groups"],
                          "groups": len(classified["control_groups"]),
                          "required_arms": list(cal.COMPARATOR_ARMS[_target_kind(target)]),
                          "incomplete": classified["incomplete_groups"],
                          "details": classified["control_groups"]},
        "release_windows": {"estimate": True, "count": len(windows),
                            "release_weeks_per_month": release_weeks,
                            "days_to_next": days_to_next,
                            "current_week_is_window": current_week_is_window,
                            "windows": windows, "next": windows[0] if windows else None},
        "projection": projection,
        "missing": missing,
        "checklist": steps,
        "registration": {"can_register": can_register,
                         "release_confirmed": bool(release_confirmed),
                         "current_week": week,
                         "current_week_rows": week_rows,
                         "prospective_origin": prospective_origin,
                         "prospective_target_period": prospective_period["label"],
                         "cannot_register": blockers,
                         "warnings": gate_warnings},
        "alerts": alerts,
        "rules": {"min_forecasts": cal.MIN_FORECASTS,
                  "min_distinct_periods": cal.MIN_DISTINCT_PERIODS,
                  "control_arms": list(cal.COMPARATOR_ARMS[_target_kind(target)]),
                  "one_row_per_week": "один целевой месяц даёт одну строку на неделю",
                  "unresolved_late_days": cal.UNRESOLVED_LATE_DAYS,
                  "unresolved_alert_days": cal.UNRESOLVED_ALERT_DAYS,
                  "cadence_warn_days": CADENCE_WARN_DAYS,
                  "cadence_alert_days": CADENCE_ALERT_DAYS,
                  "reference": "docs/202 runbook, docs/139, docs/119, protocol v5 (C6)"},
        "limitations": [
            "календарь релизных недель и дата гейта — оценка по средней частоте 2,5 релизных "
            "недель/мес и лагу разрешения; это не обещание даты",
            "релизная неделя — человеческое решение: runbook не знает календарь BLS/BEA и без "
            "--confirm-release всегда показывает RELEASE_NOT_CONFIRMED",
            "счётчики 12/4 считаются по журналу и первой публикации факта; loss/coverage даёт "
            "tools/registry_resolve.py по разрешённой выборке",
            "строки, отклонённые по целостности (нет контрольной группы, look-ahead, чужая "
            "vintage_week), не входят в счётчики и не удаляются из журнала — журнал append-only",
            "runbook ничего не пишет в журнал, аудит и статус; --out пишет только c6_runbook.*",
            "решение о регистрации, коммите журнала и статусе кандидата принимает Lead; "
            "runbook — подготовка, а не разрешение",
        ],
    }


# --------------------------------------------------------------------------------------- --check
def build_check(*, journal: Path = cal.DEFAULT_JOURNAL, status: Path = cal.DEFAULT_STATUS,
                audit: Path = cal.DEFAULT_AUDIT, as_of=None,
                candidate: str = cal.DEFAULT_CANDIDATE, target: str = cal.DEFAULT_TARGET,
                horizon: int = cal.DEFAULT_HORIZON, fact_lookup=None) -> dict:
    """Validate the inputs and the journal/status consistency (never writes anything)."""
    problems: list[dict] = []
    warnings: list[dict] = []

    def problem(code: str, message: str) -> None:
        problems.append({"code": code, "message": message})

    def caution(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    rows: list[dict] = []
    status_doc = None
    journal_ok = status_ok = False
    try:
        rows = _read_journal(journal)
        journal_ok = True
    except C6RunbookError as exc:
        problem("JOURNAL_UNREADABLE", str(exc))
    try:
        status_doc = cal.load_status(status)
        status_ok = True
    except cal.C6CalendarError as exc:
        problem("STATUS_UNREADABLE", str(exc))
    try:
        audit_events = cal.read_audit(audit)
    except cal.C6CalendarError as exc:
        problem("AUDIT_UNREADABLE", str(exc))
        audit_events = []

    report = {"schema": CHECK_SCHEMA, "ok": False, "as_of": None, "week": None,
              "journal": _repo_path(journal), "status": _repo_path(status),
              "audit": _repo_path(audit), "problems": problems, "warnings": warnings,
              "journal_valid": journal_ok, "status_valid": status_ok,
              "checked": {"journal_rows": len(rows), "audit_events": len(audit_events)}}
    if not (journal_ok and status_ok):
        return report

    if as_of is None:
        as_of = _parse_date(status_doc.get("date"), "status.date")
    else:
        as_of = _parse_date(as_of, "as_of")
    horizon = _int(horizon, "horizon", minimum=0)
    week = cal.iso_week_label(as_of)
    report["as_of"] = as_of.isoformat()
    report["week"] = week

    status_date = _parse_date(status_doc.get("date"), "status.date")
    if status_date > as_of:
        problem("STATUS_NEWER_THAN_REPORT",
                f"статус от {status_date} позже отчётной даты {as_of}")
    elif status_date < as_of:
        caution("STATUS_STALE",
                f"статус от {status_date} старше отчётной даты {as_of}: счётчики могут отставать")
    pair = status_doc.get("pair") or {}
    if (status_doc.get("candidate") or {}).get("id") != candidate:
        problem("CANDIDATE_MISMATCH",
                f"статус описывает кандидата {(status_doc.get('candidate') or {}).get('id')!r}, "
                f"а проверяется {candidate!r}")
    if pair.get("target") != target or _horizon(pair) != horizon:
        problem("PAIR_MISMATCH",
                f"статус описывает пару {pair.get('target')}/h={pair.get('horizon')}, "
                f"а проверяется {target}/h={horizon}")

    later = [row for row in rows if _registered_day(row) is not None
             and _registered_day(row) > as_of]
    if later:
        problem("ROWS_AFTER_REPORT",
                f"{len(later)} строк(и) журнала зарегистрированы позже отчётной даты {as_of}: "
                "выберите --date не раньше последней регистрации")

    classified = classify_rows(rows=rows, candidate=candidate, target=target, horizon=horizon,
                              as_of=as_of, fact_lookup=fact_lookup)
    entries = classified["entries"]
    journal_counts = {"rows_total": len(rows), "rows_candidate": len(entries),
                      "n_resolved": classified["n_resolved"],
                      "distinct_target_periods": len(classified["periods_all"]),
                      "pending": len(classified["buckets"]["PENDING"]),
                      "rejected": len(classified["buckets"]["REJECTED"])}
    report["journal_counts"] = journal_counts
    status_journal = status_doc.get("journal") or {}
    status_c6 = status_doc.get("c6") or {}
    report["status_counts"] = {"rows_total": status_journal.get("rows_total"),
                               "rows_candidate": status_journal.get("rows_candidate"),
                               "n_resolved": status_c6.get("n_resolved"),
                               "distinct_target_periods":
                                   status_c6.get("distinct_target_periods")}

    if status_journal.get("rows_total") != len(rows):
        problem("ROWS_TOTAL_MISMATCH",
                f"статус: {status_journal.get('rows_total')} строк всего, журнал: {len(rows)}")
    if status_journal.get("rows_candidate") != len(entries):
        problem("ROWS_CANDIDATE_MISMATCH",
                f"статус: {status_journal.get('rows_candidate')} строк кандидата, "
                f"журнал: {len(entries)}")
    if classified["facts_available"]:
        if status_c6.get("n_resolved") != classified["n_resolved"]:
            problem("N_RESOLVED_MISMATCH",
                    f"статус: {status_c6.get('n_resolved')} разрешённых, "
                    f"журнал: {classified['n_resolved']}")
    else:
        caution("FACTS_UNAVAILABLE",
                "винтажный архив недоступен: n_resolved сверяется только по статусу")
    if status_c6.get("distinct_target_periods") != len(classified["periods_all"]):
        problem("DISTINCT_MISMATCH",
                f"статус: {status_c6.get('distinct_target_periods')} периодов, "
                f"журнал: {len(classified['periods_all'])}")

    for entry in entries:
        if "SUPERSEDED_BY_CORRECTION" in entry["codes"]:
            caution("SUPERSEDED_ROW",
                    f"{entry['ref']}: заменена коррекцией, в счётчики не входит")
        for code in entry["codes"]:
            if code in ("MISSING_VINTAGE_WEEK", "VINTAGE_WEEK_MISMATCH",
                        "MISSING_REGISTERED_AT", "PUBLISHED_BEFORE_REGISTRATION", "BAD_ORIGIN",
                        "BAD_VINTAGE_WEEK"):
                problem(code, entry["reasons"][entry["codes"].index(code)])
            elif code == "CONTROL_GROUP_MISSING":
                problem("CONTROL_GROUP_MISSING",
                        f"{entry['ref']}: нет контрольных плеч {entry['missing_arms']}")
            elif code == "ROW_AFTER_REPORT":
                problem("ROW_AFTER_REPORT", entry["reasons"][entry["codes"].index(code)])
    if classified["buckets"]["BLOCKED"]:
        caution("FUTURE_FACT_BLOCKED",
                f"{len(classified['buckets']['BLOCKED'])} строк(и) ждут первой публикации "
                "позже отчётной даты (no look-ahead)")
    checked_weeks = {event["week"] for event in audit_events if event.get("event") == "WEEK_CHECK"}
    report["checked"]["week_checked"] = week in checked_weeks
    if not Path(audit).is_file():
        caution("NO_AUDIT", "аудит-лог отсутствует: история недель не проверяется")
    elif week not in checked_weeks:
        caution("NO_WEEK_CHECK", f"за неделю {week} нет WEEK_CHECK")
    report["ok"] = not problems
    return report


# -------------------------------------------------------------------------------------- rendering
def _repo_path(path: Path) -> str:
    try:
        return str(Path(path).relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def render_markdown(document: dict) -> str:
    c6 = document["c6"]
    rows = document["rows"]
    registration = document["registration"]
    lines = [
        f"# C6 runbook — неделя {document['week']['label']} ({document['as_of']})",
        "",
        f"Кандидат `{document['pair']['candidate']}`, пара `{document['pair']['target']}` / "
        f"h={document['pair']['horizon']}. Журнал: `{document['source']['journal']}` "
        f"(SHA-256 `{(document['source']['journal_sha256'] or '-')[:12]}…`).",
        "",
        f"**Статус 12/4: {c6['status_line']} — `{c6['status']}`. Loss/coverage не проверялись.**",
        "",
        "## 1. Счётчики C6",
        "",
        "| Критерий | Текущее | Требуется | Осталось |",
        "|---|---:|---:|---:|",
        f"| Разрешённых прогнозов | {c6['n_resolved']} | {c6['min_forecasts']} | "
        f"{c6['remaining_forecasts']} |",
        f"| Различных целевых периодов | {c6['distinct_target_periods']} | "
        f"{c6['min_distinct_periods']} | {c6['remaining_periods']} |",
        f"| Различных периодов среди разрешённых | {c6['distinct_resolved_periods']} | — | — |",
        f"| Ждут факта / заблокированы / отклонены | {c6['pending']} / {c6['blocked']} / "
        f"{c6['rejected']} | — | — |",
        "",
        "## 2. Строки журнала: разрешённые и отклонённые",
        "",
        f"Всего строк кандидата: {rows['total']}; разрешено {rows['counts']['allowed']}, "
        f"ждут факта {rows['counts']['pending']}, заблокировано "
        f"{rows['counts']['blocked']}, отклонено {rows['counts']['rejected']}.",
        "",
        "| Строка | Целевой период | Состояние | Причина |",
        "|---|---|---|---|",
    ]
    for entry in document["rows"]["entries"]:
        reason = "; ".join(entry["reasons"]) if entry["reasons"] else "—"
        lines.append(f"| `{entry['ref']}` | {entry['target_period'] or '—'} | "
                     f"{entry['state']} | {reason} |")
    lines += [
        "",
        "## 3. Ближайшие релизные окна (оценка)",
        "",
        "| # | неделя | начало | оценка периода | разрешится к | новое окно |",
        "|---|---|---|---|---|---|",
    ]
    for window in document["release_windows"]["windows"]:
        lines.append(f"| {window['sequence']} | `{window['week']}` | {window['week_start']} | "
                     f"{window['estimated_target_period']} | {window['resolves_by']} | "
                     f"{'да' if window['adds_distinct_period'] else 'нет'} |")
    projection = document["projection"]
    lines += [
        "",
        f"- 12-й разрешённый прогноз (оценка): {projection['date_12th_resolved'] or '—'};",
        f"- 4-й различный целевой период (оценка): {projection['date_4th_period'] or '—'};",
        f"- ориентир гейта C6 (оценка): {projection['gate_date'] or '—'};",
        f"- календарь — оценка: {projection['note']}.",
        "",
        "## 4. Чего не хватает",
        "",
    ]
    for item in document["missing"]:
        lines.append(f"- `{item['code']}` — {item.get('note') or item}")
    lines += ["", "## 5. Нельзя регистрировать на этой неделе", ""]
    if registration["cannot_register"]:
        lines += [f"- **{item['code']}** — {item['message']}"
                  for item in registration["cannot_register"]]
    else:
        lines.append("- блокирующих причин нет: строка может быть зарегистрирована "
                     "при подтверждённом релизе")
    if registration["warnings"]:
        lines += ["", "Предупреждения регистрации:", ""]
        lines += [f"- `{item['code']}` — {item['message']}" for item in registration["warnings"]]
    lines += [
        "",
        f"Итог: **можно регистрировать: {'да' if registration['can_register'] else 'нет'}**; "
        f"релиз подтверждён человеком: "
        f"{'да' if registration['release_confirmed'] else 'нет'}.",
        "",
        "## 6. Чек-лист недели",
        "",
        "| # | Шаг | Статус | Команда / примечание |",
        "|---|---|---|---|",
    ]
    for step in document["checklist"]:
        detail = step.get("command") or step.get("note") or "—"
        lines.append(f"| {step['step']} | {step['action']} | {step['status']} | `{detail}` |")
    lines += ["", "## 7. Алерты", ""]
    if document["alerts"]:
        lines += [f"- **{item['severity']}** `{item['code']}` — {item['message']}"
                  for item in document["alerts"]]
    else:
        lines.append("- алертов нет")
    lines += ["", "## 8. Ограничения", ""]
    lines += [f"- {item}" for item in document["limitations"]]
    lines += ["", "Сгенерировано `tools/c6_runbook.py`; журнал, аудит и статус не изменяются, "
                  "сырые значения фактов не выводятся.", ""]
    return "\n".join(lines)


def render_check_lines(document: dict) -> str:
    lines = [f"C6 runbook --check ({document.get('as_of') or '—'}, "
             f"{document.get('week') or '—'}): "
             f"{'OK' if document['ok'] else 'ПРОБЛЕМЫ'}",
             f"  journal_valid={str(document['journal_valid']).lower()}, "
             f"status_valid={str(document['status_valid']).lower()}, "
             f"строк в журнале: {document['checked'].get('journal_rows')}"]
    for item in document["problems"]:
        lines.append(f"  [PROBLEM] {item['code']}: {item['message']}")
    for item in document["warnings"]:
        lines.append(f"  [WARN] {item['code']}: {item['message']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- I/O
def write_runbook(document: dict, out_path: Path) -> list[str]:
    out_path = Path(out_path)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    markdown = render_markdown(document)
    if out_path.suffix.lower() == ".json":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8", newline="\n")
        return [str(out_path)]
    if out_path.suffix.lower() == ".md":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8", newline="\n")
        return [str(out_path)]
    out_path.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_path / RUNBOOK_JSON, out_path / RUNBOOK_MD
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(markdown, encoding="utf-8", newline="\n")
    return [str(json_path), str(md_path)]


def render_summary(document: dict) -> str:
    c6 = document["c6"]
    registration = document["registration"]
    lines = [f"C6 runbook ({document['as_of']}, {document['week']['label']}): "
             f"{c6['status_line']} — {c6['status']}",
             f"  строки: разрешено {c6['n_resolved']}, ждут факта {c6['pending']}, "
             f"заблокировано {c6['blocked']}, отклонено {c6['rejected']}"]
    window = document["release_windows"]["next"]
    if window:
        lines.append(f"  ближайшее окно: {window['week']} ({window['week_start']}), "
                     f"оценка периода {window['estimated_target_period']}")
    projection = document["projection"]
    lines.append(f"  12-й прогноз (оценка): {projection['date_12th_resolved']}; "
                 f"4-й период (оценка): {projection['date_4th_period']}; "
                 f"гейт (оценка): {projection['gate_date']}")
    if registration["cannot_register"]:
        for item in registration["cannot_register"]:
            lines.append(f"  нельзя регистрировать: {item['code']} — {item['message']}")
    else:
        lines.append("  нельзя регистрировать: блокирующих причин нет")
    gaps = ", ".join(f"{item['code']} {item.get('remaining', '')}".strip()
                     for item in document["missing"] if item["code"] in
                     ("FORECASTS", "PERIODS"))
    if gaps:
        lines.append(f"  не хватает: {gaps}")
    for item in document["alerts"]:
        lines.append(f"  [{item['severity']}] {item['code']}: {item['message']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Weekly C6 runbook: counters 12/4, rows, release windows, gaps, checklist "
                    "and registration gate (docs/202, docs/139)")
    parser.add_argument("--journal", default=str(cal.DEFAULT_JOURNAL),
                        help="forward journal (default registry/forecasts.jsonl)")
    parser.add_argument("--status", default=str(cal.DEFAULT_STATUS),
                        help="shadow status document (default build/shadow/shadow_status.json)")
    parser.add_argument("--audit", default=str(cal.DEFAULT_AUDIT),
                        help="operational audit log (default build/c6/c6_audit.jsonl)")
    parser.add_argument("--date", default=None,
                        help="report date YYYY-MM-DD (default: status.date; no clock is read)")
    parser.add_argument("--candidate", default=cal.DEFAULT_CANDIDATE,
                        help="candidate id of the reported pair (default %(default)s)")
    parser.add_argument("--target", default=cal.DEFAULT_TARGET,
                        help="target of the reported pair (default %(default)s)")
    parser.add_argument("--horizon", type=int, default=cal.DEFAULT_HORIZON,
                        help="horizon of the reported pair (default %(default)s)")
    parser.add_argument("--resolution-lag-days", dest="resolution_lag_days", type=int,
                        default=cal.DEFAULT_RESOLUTION_LAG_DAYS,
                        help="assumed days between registration and resolution (default 30)")
    parser.add_argument("--confirm-release", dest="confirm_release", action="store_true",
                        help="the human confirmed a first data release for the report week")
    parser.add_argument("--check", action="store_true",
                        help="validate the inputs and the journal/status consistency; exit 1 on "
                             "problems")
    parser.add_argument("--json", action="store_true", help="print the document as JSON")
    parser.add_argument("--out", default=None,
                        help="write the runbook: directory (json+md), .json or .md path")
    args = parser.parse_args(argv)

    try:
        as_of = _parse_date(args.date, "--date") if args.date else None
    except C6RunbookError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    if args.check:
        try:
            document = build_check(journal=Path(args.journal), status=Path(args.status),
                                   audit=Path(args.audit), as_of=as_of, candidate=args.candidate,
                                   target=args.target, horizon=args.horizon)
        except (C6RunbookError, cal.C6CalendarError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        if args.json:
            print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1))
        else:
            print(render_check_lines(document))
        return 0 if document["ok"] else 1

    try:
        document = build_runbook(journal=Path(args.journal), status=Path(args.status),
                                 audit=Path(args.audit), as_of=as_of, candidate=args.candidate,
                                 target=args.target, horizon=args.horizon,
                                 release_confirmed=args.confirm_release,
                                 resolution_lag_days=args.resolution_lag_days)
    except (C6RunbookError, cal.C6CalendarError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.out:
        document["written"] = write_runbook(document, Path(args.out))
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1))
        return 0
    print(render_summary(document))
    for path in document.get("written", []):
        print(f"  записано: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
