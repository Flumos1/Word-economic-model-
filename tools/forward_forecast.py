#!/usr/bin/env python3
"""Forward-registry engine: build one monthly origin of registered forecasts and append it.

Protocol v4 (``protocols/evaluation_protocol_v4.json`` -> ``forward_registry``):

  * cadence: monthly, after the last data release of the month;
  * record: candidate_id, code_commit, data_vintage_hash, target, horizon, origin,
    quantiles_5_to_95_step_5 (T1/T2) or event_probability (T3), point, scoring_rule_commit;
  * control arms: RW, EQUAL_WEIGHT_POINT, EQUAL_WEIGHT_DENSITY, OWN_NOWCAST_BRIDGE;
  * integrity: committed before the actual value is published, append-only.

The tool builds the rows of one origin for the registered candidates (ensemble layer, the
``amfe.candidates`` candidate and the SPF-augmented candidates) and for the protocol control
arms, validates them against ``tools/registry.py`` and appends them there.  The journal is the
only output that is written; the optional ``--out`` file holds the same rows for CI artifacts.

Guards (all refusals are hard, no partial row is written):

  * the origin month is in the future (later than the current month) -> refuse;
  * the origin is before the journal start (the first ``registered_at``, i.e. no backdating)
    -> refuse;
  * the fact of the target period is already published (first-publication actual exists, or
    the period is at or before the last observation of the source series) -> refuse.

Pre-origin recording
--------------------
The protocol origin is the end of a calendar month; the monthly workflow runs after the CPI
release of the previous month (around the 15th), i.e. before that month-end.  Such a row is a
*pre-origin* row: it is committed before the fact, uses only data published at the recording
snapshot (strictly a subset of the information set of the month-end origin), and the engine
labels it in ``notes``.  The information cutoff is the latest observation in the data snapshot;
no observation after it enters a forecast (``amfe.forecast`` enforces this and raises on any
look-ahead).

Data modes
----------
``--mode latest`` (default) uses the latest release tapes restored from the private
``data-refresh`` artifacts: the first-release tape class of protocol v4
(``data_depth.classification_rule``, ``REAL_TIME_TAPE``).  ``--mode auto``/``--mode vintage``
use the ALFRED vintage archive when it covers the origin and are the reproducible REAL_TIME
alternative; the archive is quarterly, so the newest vintage can lag the tape by a release.
The chosen mode and the resulting class are recorded in ``notes``.

Usage:
    python tools/forward_forecast.py --origin 2026-11
    python tools/forward_forecast.py --origin 2026-11 --candidates ENSEMBLE_ADAPTIVE \
        --targets T1_CPI_YOY:1 --out build/forward_records.json
    python tools/forward_forecast.py --origin 2026-11 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"

QUANTILE_GRID = [round(0.05 * i, 2) for i in range(1, 20)]
CONTROL_ARM_IDS = ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY", "OWN_NOWCAST_BRIDGE")
DEFAULT_CANDIDATES = ("ENSEMBLE_ADAPTIVE", "ENSEMBLE_INVERSE_MSE")
DEFAULT_TARGETS = ("T1_CPI_YOY:1",)


class ForwardEngineError(RuntimeError):
    """The origin, a candidate or a record cannot be produced (hard refusal)."""


# --------------------------------------------------------------------------- provenance
def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def protocol_commit() -> str:
    """SHA-256 of the frozen protocol text (line endings normalised, as in the harness)."""
    return sha256_bytes(PROTOCOL.read_bytes().replace(b"\r\n", b"\n"))


def code_commit() -> str:
    """Current git commit; the GITHUB_SHA fallback covers a detached CI checkout."""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, timeout=20)
        value = result.stdout.strip()
        if result.returncode == 0 and 7 <= len(value) <= 64:
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    env = os.environ.get("GITHUB_SHA", "").strip()
    if env:
        return env
    raise ForwardEngineError("cannot determine the git commit (code_commit)")


def registered_at_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- pre-origin data context
class _PreOriginData:
    """Allow ``amfe.forecast`` to build a context at the recording origin.

    ``amfe.forecast.data.validate_origin`` caps the origin at the last observation of the
    downloaded tapes.  A monthly row recorded on the 15th has an origin at that month-end,
    one month after the last observation, and must still be a date-labelled forecast for the
    coming month.  The patch widens the cap by exactly one month for the recording origin;
    every model then only sees observations at or before the last tape observation (all PIT
    assertions stay active).  Historical origins keep the original semantics.
    """

    def __init__(self, recording_origin: date) -> None:
        from amfe.forecast import data as data_module

        self._data = data_module
        self._original = data_module.validate_origin
        self._limit = data_module.month_index(data_module.month_end_of(recording_origin))

    def __enter__(self):
        data_module = self._data
        original = self._original
        limit = self._limit

        def validate_origin(origin, data_root=None):
            origin_d = data_module.month_end_of(origin)
            first, last = data_module.latest_tape_bounds(data_root)
            om = data_module.month_index(origin_d)
            if om < first or om > limit:
                return original(origin, data_root)
            if om <= last + 1:
                return origin_d
            return original(origin, data_root)

        data_module.validate_origin = validate_origin
        return self

    def __exit__(self, exc_type, exc, tb):
        self._data.validate_origin = self._original
        return False


def data_vintage_hash(origin: str, mode: str) -> str:
    """SHA-256 fingerprint of the vintage dates / last observations used at the origin.

    Only dates and period labels enter the payload: never a raw value.  The fingerprint is the
    canonical-JSON SHA-256, the same convention as ``tools/registry.py`` record digests.
    """
    from amfe.forecast.data import DataContext, month_end_of

    with _PreOriginData(month_end_of(origin)):
        context = DataContext.from_local(month_end_of(origin), mode=mode)
    payload = {
        "mode": mode,
        "origin": month_end_of(origin).isoformat(),
        "series": {
            name: {
                "vintage": (meta.vintage_date.isoformat() if meta.vintage_date else None),
                "last_observation": context.series[name].period_label(),
            }
            for name, meta in sorted(context.meta.items())
        },
    }
    return sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8"))


def info_cutoff(origin: str) -> dict:
    """Metadata of the recording snapshot: last observation labels and vintage dates."""
    from amfe.forecast.data import DataContext, month_end_of

    with _PreOriginData(month_end_of(origin)):
        context = DataContext.from_local(month_end_of(origin), mode="latest")
    return {
        "last_observations": {name: context.series[name].period_label()
                              for name in sorted(context.series)},
        "vintages": {name: (meta.vintage_date.isoformat() if meta.vintage_date else None)
                     for name, meta in sorted(context.meta.items())},
    }


# ------------------------------------------------------------------------------- guards
def parse_period(spec: str) -> tuple[str, str]:
    """``T1_CPI_YOY:1`` -> ("T1_CPI_YOY", "1"); a bare target defaults to its first horizon."""
    from registry import TARGETS

    parts = str(spec).strip().split(":")
    target = parts[0].strip()
    if target not in TARGETS:
        raise ForwardEngineError(f"unknown target {target!r}; known: {', '.join(sorted(TARGETS))}")
    if len(parts) == 1:
        return target, "default"
    if len(parts) != 2 or not parts[1].strip():
        raise ForwardEngineError(f"bad target spec {spec!r}; expected TARGET or TARGET:HORIZON")
    horizon = parts[1].strip()
    if not horizon.lstrip("-").isdigit():
        raise ForwardEngineError(f"horizon in {spec!r} is not an integer")
    if int(horizon) not in TARGETS[target]["horizons"]:
        raise ForwardEngineError(f"horizon {horizon} is not in {TARGETS[target]['horizons']} for {target}")
    return target, horizon


def journal_start_month(journal: Path) -> str | None:
    """Journal start = month of the first ``registered_at`` (no backdating)."""
    from registry import read_rows

    dates = sorted(row["registered_at"] for row in read_rows(journal) if row.get("registered_at"))
    return dates[0][:7] if dates else None


def _last_observation_index(target: str) -> int | None:
    """Period index of the last published observation of the target's source series."""
    from amfe.forecast.data import load_latest

    name = {"T1_CPI_YOY": "CPIAUCSL", "T2_GDP_GROWTH": "GDPC1"}.get(target)
    if name is None:
        return None
    try:
        series = load_latest(name)
    except Exception:  # pragma: no cover - data snapshot without the series
        return None
    return series.last_index


def fact_available(target: str, origin: str, horizon: int) -> dict:
    """Is the actual value of the target period already published? (no values are reported)"""
    from amfe.evaluation.actuals import actual_pair
    from amfe.forecast.data import month_end_of, target_period

    pair = actual_pair(target, month_end_of(origin), horizon)
    if pair.get("primary") is not None:
        return {"published": True, "reason": f"first-publication actual exists ({pair.get('primary_vintage')})"}
    freq, period = target_period(target, month_end_of(origin), horizon)
    last = _last_observation_index(target)
    if last is not None and int(period) <= int(last):
        return {"published": True,
                "reason": f"target period {period} is at or before the last observation ({last})"}
    return {"published": False, "reason": None}


def check_origin(origin: str, journal: Path) -> dict:
    """Validate the origin against the calendar, the journal start and the published facts."""
    from amfe.forecast.data import month_end_of, month_index, month_label

    try:
        origin_d = month_end_of(origin)
    except (TypeError, ValueError) as error:
        raise ForwardEngineError(f"bad origin {origin!r}: {error}") from error
    month = month_label(month_index(origin_d))
    today = date.today()
    current_month = f"{today.year:04d}-{today.month:02d}"
    start = journal_start_month(journal) or current_month
    if month > current_month:
        raise ForwardEngineError(
            f"origin {month} is in the future (the journal is written in or after the month): "
            f"current month is {current_month}")
    if month < start:
        raise ForwardEngineError(
            f"origin {month} is before the journal start {start}: the journal is append-only and "
            f"backdating is forbidden")
    pre_origin = origin_d > today
    return {"origin": month, "origin_date": origin_d.isoformat(), "pre_origin": pre_origin,
            "current_month": current_month, "journal_start": start}


# -------------------------------------------------------------------------- candidates
def _dispatcher():
    from amfe.candidates.models import CANDIDATE_IDS
    from amfe.candidates.models import candidate_forecast as rw_ar_candidate
    from amfe.ensemble import ENSEMBLE_IDS
    from amfe.ensemble.ensemble import candidate_forecast as ensemble_candidate
    try:
        from spf_augmented_candidate import CANDIDATE_IDS as SPF_IDS
        from spf_augmented_candidate import candidate_forecast as spf_candidate
    except Exception:  # pragma: no cover - optional tool
        SPF_IDS = ()
        spf_candidate = None

    def run(candidate_id, target, origin, horizon, mode):
        if candidate_id in ENSEMBLE_IDS:
            return ensemble_candidate(candidate_id, target, origin, horizon, mode=mode)
        if SPF_IDS and candidate_id in SPF_IDS:
            return spf_candidate(candidate_id, target, origin, horizon, mode=mode)
        if candidate_id in CANDIDATE_IDS:
            return rw_ar_candidate(candidate_id, target, origin, horizon, mode=mode)
        raise ForwardEngineError(f"unknown candidate {candidate_id!r}")

    return run, set(ENSEMBLE_IDS) | set(CANDIDATE_IDS) | set(SPF_IDS or ())


def _composition_members(target: str, horizon: int, loss: str) -> list[str]:
    composition = json.loads(COMPOSITION.read_text(encoding="utf-8"))
    block = composition["targets"][target]["losses"][loss]
    entry = block.get("by_horizon", {}).get(str(int(horizon)), {})
    members = entry.get("members") or [m["id"] if isinstance(m, dict) else m
                                       for m in block.get("members", [])]
    return list(members)


def control_arm_forecasts(target: str, origin: str, horizon: int, mode: str,
                          codes: dict) -> list[dict]:
    """Protocol control arms that are defined for this pair (derived statistics of our models)."""
    from amfe.evaluation.distributions import mixture_quantiles
    from amfe.forecast import forecast
    from amfe.forecast.data import month_end_of
    from amfe.forecast.models import MODEL_IDS, MODEL_TARGETS

    origin_date = month_end_of(origin)
    rows = []
    # RW: random walk, T1 only (protocol origin_policy.t1_horizon: last published y/y value)
    if target == "T1_CPI_YOY" and "RW" in MODEL_TARGETS:
        member = forecast("RW", target, origin_date, int(horizon), mode=mode)
        rows.append({"candidate_id": "RW", "target": target, "horizon": int(horizon),
                     "point": float(member.point),
                     "quantiles": [float(v) for v in member.quantiles],
                     "event_probability": None})
    # OWN_NOWCAST_BRIDGE: the project's own T2 h=0 nowcast only
    if target == "T2_GDP_GROWTH" and int(horizon) == 0:
        member = forecast("OWN_NOWCAST_BRIDGE", target, origin_date, 0, mode=mode)
        rows.append({"candidate_id": "OWN_NOWCAST_BRIDGE", "target": target, "horizon": 0,
                     "point": float(member.point),
                     "quantiles": [float(v) for v in member.quantiles],
                     "event_probability": None})
    # EQUAL_WEIGHT_POINT / EQUAL_WEIGHT_DENSITY over the frozen comparator composition
    loss = "Brier" if target == "T3_RECESSION" else "CRPS"
    members = [m for m in _composition_members(target, horizon, loss) if m in MODEL_IDS]
    parts = []
    for member_id in members:
        try:
            parts.append(forecast(member_id, target, origin_date, int(horizon), mode=mode))
        except Exception:
            continue
    if parts:
        if target == "T3_RECESSION":
            probability = sum(float(p.probability) for p in parts) / len(parts)
            rows.append({"candidate_id": "EQUAL_WEIGHT_POINT", "target": target,
                         "horizon": int(horizon), "point": None, "quantiles": None,
                         "event_probability": probability})
            rows.append({"candidate_id": "EQUAL_WEIGHT_DENSITY", "target": target,
                         "horizon": int(horizon), "point": None, "quantiles": None,
                         "event_probability": probability})
        else:
            points = [float(p.point) for p in parts if p.point is not None]
            distributions = [p.quantiles for p in parts if p.quantiles is not None]
            if points and distributions:
                mixed = [float(v) for v in mixture_quantiles(distributions)]
                mean_point = float(sum(points) / len(points))
                for arm in ("EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"):
                    rows.append({"candidate_id": arm, "target": target,
                                 "horizon": int(horizon), "point": mean_point,
                                 "quantiles": list(mixed), "event_probability": None})
    return rows


# ----------------------------------------------------------------------------- records
def build_record(candidate_id: str, target: str, horizon: int, origin: str, *,
                 commit: str, vintage: str, scoring: str, point=None, quantiles=None,
                 event_probability=None, registered_at: str | None = None,
                 control_arm: bool = False, notes: str = "") -> dict:
    """Assemble one protocol row; no validation here (tools/registry.py validates)."""
    row: dict = {
        "candidate_id": candidate_id,
        "code_commit": commit,
        "data_vintage_hash": vintage,
        "target": target,
        "horizon": int(horizon),
        "origin": origin,
        "scoring_rule_commit": scoring,
        "registered_at": registered_at or registered_at_now(),
    }
    if target == "T3_RECESSION":
        row["event_probability"] = float(event_probability)
        row["point"] = None
    else:
        row["quantiles_5_to_95_step_5"] = [float(v) for v in quantiles]
        row["point"] = float(point)
    if control_arm:
        row["control_arm"] = True
    if notes:
        row["notes"] = notes
    return row


def forecast_rows(candidates: list[str], targets: list[tuple[str, int]], origin: str,
                  mode: str, *, commit: str, vintage: str, scoring: str,
                  registered_at: str | None, data_class: str,
                  info: dict, pre_origin: bool) -> list[dict]:
    run, known = _dispatcher()
    from amfe.forecast.data import month_end_of

    rows: list[dict] = []
    cutoff = ",".join(f"{name}={label}" for name, label in list(info["last_observations"].items())[:6])
    protocol_class = "REAL_TIME_TAPE" if mode == "latest" else data_class
    notes_base = (f"origin {origin}; mode={mode}; protocol class {protocol_class}; "
                  f"forecast-layer data class {data_class}; "
                  f"pre_origin={str(pre_origin).lower()}; info cutoff: {cutoff}")
    for candidate_id in candidates:
        if candidate_id not in known:
            raise ForwardEngineError(f"unknown candidate {candidate_id!r}")
    with _PreOriginData(month_end_of(origin)):
        for target, horizon in targets:
            for candidate_id in candidates:
                result = run(candidate_id, target, month_end_of(origin), int(horizon), mode)
                rows.append(build_record(
                    candidate_id, target, horizon, origin, commit=commit, vintage=vintage,
                    scoring=scoring, point=result.get("point"),
                    quantiles=result.get("quantiles"),
                    event_probability=result.get("probability"),
                    registered_at=registered_at, notes=notes_base))
            for arm in control_arm_forecasts(target, origin, horizon, mode, known):
                rows.append(build_record(
                    arm["candidate_id"], target, horizon, origin, commit=commit, vintage=vintage,
                    scoring=scoring, point=arm.get("point"), quantiles=arm.get("quantiles"),
                    event_probability=arm.get("event_probability"),
                    registered_at=registered_at, control_arm=True,
                    notes=notes_base + "; protocol control arm"))
    return rows


def append_rows(rows: list[dict], journal: Path, *, dry_run: bool = False) -> dict:
    """Validate every row first, then append.  A duplicate with the same digest is skipped."""
    from registry import RegistryError, append_record, read_rows, validate_record

    problems = []
    for row in rows:
        problems.extend(f"{row.get('candidate_id')}/{row.get('target')}/{row.get('horizon')}: "
                        f"{problem}" for problem in validate_record(row))
    if problems:
        raise ForwardEngineError("invalid record(s): " + "; ".join(problems))
    existing = {row.get("candidate_id") + "|" + str(row.get("target")) + "|"
                + str(row.get("horizon")) + "|" + str(row.get("origin")): row
                for row in read_rows(journal)}
    appended, skipped = [], []
    for row in rows:
        key = (row["candidate_id"] + "|" + str(row["target"]) + "|" + str(row["horizon"])
               + "|" + str(row["origin"]))
        if key in existing:
            from registry import record_digest

            if record_digest(existing[key]) != record_digest(row):
                raise ForwardEngineError(
                    f"identity {row['candidate_id']}/{row['target']}/{row['horizon']}/{row['origin']} "
                    f"already exists with different content: append-only, use a correction row")
            skipped.append(row["candidate_id"] + "|" + str(row["target"]))
            continue
        if not dry_run:
            result = append_record(row, journal)
            appended.append({"candidate_id": row["candidate_id"], "target": row["target"],
                             "horizon": row["horizon"], "origin": row["origin"],
                             "digest": result["digest"],
                             "point": row.get("point"),
                             "event_probability": row.get("event_probability")})
        else:
            from registry import record_digest

            appended.append({"candidate_id": row["candidate_id"], "target": row["target"],
                             "horizon": row["horizon"], "origin": row["origin"],
                             "digest": record_digest(row), "point": row.get("point"),
                             "event_probability": row.get("event_probability")})
    return {"appended": appended, "skipped": skipped, "journal": str(journal)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Monthly forward-registry engine")
    parser.add_argument("--origin", required=True,
                        help="origin month-end, YYYY-MM or YYYY-MM-DD")
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES),
                        help="comma-separated candidate ids (default: %(default)s)")
    parser.add_argument("--targets", default=",".join(DEFAULT_TARGETS),
                        help="comma-separated TARGET[:HORIZON] (default: T1_CPI_YOY:1)")
    parser.add_argument("--mode", default="latest", choices=("auto", "vintage", "latest"),
                        help="forecast data mode (default: %(default)s, first-release tape)")
    parser.add_argument("--journal", default=str(ROOT / "registry" / "forecasts.jsonl"))
    parser.add_argument("--registered-at", default=None,
                        help="override the ISO registered_at (only for tests/fixtures)")
    parser.add_argument("--out", default=None, help="write the same rows to this JSON file")
    parser.add_argument("--dry-run", action="store_true", help="validate and print, do not append")
    args = parser.parse_args(argv)
    journal = Path(args.journal)

    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    if not candidates:
        raise ForwardEngineError("empty candidate list")
    targets = []
    for spec in args.targets.split(","):
        if not spec.strip():
            continue
        target, horizon = parse_period(spec)
        if horizon != "default":
            targets.append((target, int(horizon)))
        else:
            from registry import TARGETS

            targets.extend((target, int(h)) for h in TARGETS[target]["horizons"][:1])
    if not targets:
        raise ForwardEngineError("empty target list")

    origin_check = check_origin(args.origin, journal)
    for target, horizon in targets:
        published = fact_available(target, args.origin, horizon)
        if published["published"]:
            raise ForwardEngineError(
                f"{target} h={horizon} at origin {origin_check['origin']}: the fact is already "
                f"published ({published['reason']}); a forecast after the fact is worthless and "
                f"backdating is forbidden")

    from amfe.forecast.data import DataContext, month_end_of

    with _PreOriginData(month_end_of(args.origin)):
        data_class = DataContext.from_local(month_end_of(args.origin), mode=args.mode).data_class
    commit = code_commit()
    scoring = protocol_commit()
    vintage = data_vintage_hash(args.origin, args.mode)
    info = info_cutoff(args.origin)
    rows = forecast_rows(candidates, targets, origin_check["origin"], args.mode,
                         commit=commit, vintage=vintage, scoring=scoring,
                         registered_at=args.registered_at, data_class=data_class,
                         info=info, pre_origin=origin_check["pre_origin"])
    outcome = append_rows(rows, journal, dry_run=args.dry_run)
    summary = {
        "origin": origin_check["origin"],
        "pre_origin": origin_check["pre_origin"],
        "mode": args.mode,
        "data_class": data_class,
        "code_commit": commit,
        "data_vintage_hash": vintage,
        "scoring_rule_commit": scoring,
        "registered_at": args.registered_at or rows[0]["registered_at"],
        "records": outcome["appended"],
        "skipped": outcome["skipped"],
        "dry_run": bool(args.dry_run),
        "journal": outcome["journal"],
        "rows": rows,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ForwardEngineError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
