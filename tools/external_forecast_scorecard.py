#!/usr/bin/env python3
"""External forecast scorecard (task-54, UC-44; docs/174).

Question
--------
The project can compare its own candidates with each other, but it cannot honestly answer "who is
more accurate - we, the SPF, GDPNow or the NY Fed, and when".  This tool is the MVP of that answer:
a point-in-time (PIT) scorecard that scores **external forecasts** against the **first publication of
the fact** on the frozen protocol windows W1 (1990-01..2006-12) and W2 (2007-01..2019-12), using the
same target definitions, losses and reporting discipline as the project's own evaluation
(``protocols/evaluation_protocol_v5.json``).

Sources
-------
``SPF_GDP_MEDIAN``
    Philadelphia Fed Survey of Professional Forecasters, median real GDP growth.  Values are read
    only from the git-ignored ``data/raw/spf/spf_median.json`` written by ``tools/fetch_spf.py``;
    the official horizon mapping is T2 h=0/1/2/4 -> ``drgdp2/3/4/6`` (``tools/spf_mapping.py``,
    docs/86).  Licence register entry ``PHILADELPHIA_FED``: OPEN for research, but with the explicit
    condition that the values stay outside git and are never published -> scored, never published.
``NY_FED_STAFF_NOWCAST``
    Federal Reserve Bank of New York Staff Nowcast of current-quarter real GDP growth (T2 h=0).
    The adapter reads an optional local archive ``data/raw/ny_fed/nowcast.csv`` with the strict
    schema documented in ``NYFED_SCHEMA``; without it the source is ``UNAVAILABLE`` (metadata only).
    Licence register entry ``NY_FED``: OPEN; rows flagged ``RECONSTRUCTED`` are not real-time
    publications and are never scored as such.
``CLEVELAND_FED_NOWCAST``
    Federal Reserve Bank of Cleveland inflation nowcast.  Licence register entry ``CLEVELAND_FED``:
    OPEN, but the published nowcast is a current-month CPI figure, and the protocol T1 target is
    year-over-year inflation at t-1+h: there is no horizon mapping, so the source is metadata only.
``ATLANTA_FED_GDPNOW``
    Licence register entry ``ATLANTA_FED_GDPNOW``: RESTRICTED ("personal and educational use only";
    the stricter reading applies).  Values are never read, even if a local copy exists; the source is
    reported as ``RESTRICTED_NOT_USED`` and the project's own OPEN ``OWN_NOWCAST_BRIDGE`` is named as
    the substitute (protocol ``licence_policy.substitute``).

PIT rules (no look-ahead)
-------------------------
* A forecast enters the scorecard only if its **publication date is on or before the origin**
  (``origin_policy.default``: only information published on or before the origin may be used).
  For the SPF the publication date is the official news-release date of the survey quarter
  (``src/amfe/ensemble/spf_calendar.py``), never the response deadline; before 1990Q3 the SPF was
  not conducted in real time and is not used.
* The actual is the **first publication** of the target value from the ALFRED vintage archive
  (``actuals.primary``), with the 12-month vintage value reported separately as ``secondary``; the
  two are never pooled.  An actual whose first-publication vintage is at or before the origin means
  the "forecast" target was already published; such a point is rejected, not scored.
* An origin belongs to a window only if the origin **and** the target period fall inside it
  (``windows.defined_by``); origins whose target lies in the COVID exclusion window are dropped
  (``windows.origin_exclusion``).

Metrics
-------
RMSE (protocol point loss), MAE and bias (context), CRPS as twice the mean pinball loss over the 19
quantiles (``forecast_product.crps``), Brier for event probabilities, and two coverage measures:
``coverage_share`` (scored points / eligible origins - how much of the window the source covers) and
``coverage_90`` (share of facts inside the source's 5-95% interval).  A metric that the source cannot
support is reported as ``null`` with an explicit status, never silently dropped and never imputed.

Licence rule
------------
Published artefacts contain derived statistics only.  A source's values appear in the report only if
the register entry is OPEN **and** none of its conditions forbids publication; otherwise only errors,
counts and metadata are written.  No third-party value is ever written to this file.

Usage
-----
    python tools/external_forecast_scorecard.py --check          # metadata/availability summary
    python tools/external_forecast_scorecard.py                  # full run, writes the report
    python tools/external_forecast_scorecard.py --horizons 0 --max-origins 24 --out <tmp>.json

The runner is deterministic for the same data and code: ``generated_at`` is excluded from
``content_sha256``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.ensemble.spf_calendar import (  # noqa: E402
    is_real_time_quarter as spf_is_real_time_quarter,
    release_date as spf_release_date,
)
from amfe.evaluation.actuals import actual_pair  # noqa: E402
import windows as windows_module  # noqa: E402

TOOL = "tools/external_forecast_scorecard.py"
REPORT_ID = "AMFE-EXTERNAL-FORECAST-SCORECARD"
VERSION = "0.1.0"
PROTOCOL_PATH = ROOT / "protocols" / "evaluation_protocol_v5.json"
LICENCE_PATH = ROOT / "protocols" / "licence_register.json"
DEFAULT_OUT = ROOT / "protocols" / "external_forecast_scorecard.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"

#: docs/173 P0-1 acceptance criteria for this MVP.
MIN_SCORED_SOURCES = 3
MIN_SCORED_POINTS = 50
#: docs/207 M2 asks for at least two scorable sources; docs/173 asked for three.  Both are
#: reported, so meeting M2 never silently hides that the stricter MVP target is still open.
M2_MIN_SCORED_SOURCES = 2
#: Project policy (stronger than the NY Fed licence): no third-party forecast/actual values are
#: written into the committed report, even when the register allows publication.  Only aggregates,
#: counts, metadata and hashes are published.
REPORT_VALUES_POLICY = "aggregates_and_metadata_only"

TARGET_T2 = "T2_GDP_GROWTH"
SUPPORTED_TARGETS = (TARGET_T2,)

#: Protocol T2 horizon -> published SPF code (drgdp5/h=3 is off the protocol grid).
SPF_GDP_CODE_FOR_HORIZON = {0: "drgdp2", 1: "drgdp3", 2: "drgdp4", 4: "drgdp6"}
SPF_GDP_CODES = ("drgdp2", "drgdp3", "drgdp4", "drgdp5", "drgdp6")
SPF_MIDDLE_MONTHS = (2, 5, 8, 11)
SPF_REAL_TIME_START = "1990Q3"
SPF_ATTRIBUTION = (
    "Source: Federal Reserve Bank of Philadelphia, Survey of Professional Forecasters, median "
    "forecasts (https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/"
    "survey-of-professional-forecasters); research use, attribution required, values never published"
)
NYFED_ATTRIBUTION = (
    "Source: Federal Reserve Bank of New York, Staff Nowcast "
    "(https://www.newyorkfed.org/research/policy/nowcast); non-exclusive licence, attribution "
    "required, modifications marked"
)
CLEVELAND_ATTRIBUTION = (
    "Source: Federal Reserve Bank of Cleveland, Inflation Nowcasting "
    "(https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting); non-commercial use, "
    "credit required, suggested citation DOI 10.26509/frbc-inflationnowcast"
)
GDPNOW_ATTRIBUTION = (
    "Source: Federal Reserve Bank of Atlanta, GDPNow "
    "(https://www.atlantafed.org/research-and-data/data/gdpnow); terms allow personal and "
    "educational use only - the project does not use these values"
)
LIVINGSTON_ATTRIBUTION = (
    "Source: Federal Reserve Bank of Philadelphia, Livingston Survey, median forecasts "
    "(https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/livingston-survey); "
    "research use, attribution to the Federal Reserve Bank of Philadelphia, values never published"
)
LIVINGSTON_STRICT_START = "2004-06-01"

#: The Livingston adapter's local archive schema (documented, validated row by row).
LIVINGSTON_SCHEMA = {
    "path": "data/raw/livingston/livingston_t2_h0.csv (git-ignored)",
    "columns": ["survey_date", "release_date", "release_basis", "origin",
                "target_period", "horizon", "forecast"],
    "survey_date": "survey date from RGDPX.Date, YYYY-MM-DD (1 June / 1 December)",
    "release_date": "publication date: Exact if present, else Not After (PIT-safe upper bound)",
    "release_basis": "exact | not_after; anything else is rejected",
    "origin": "quarter end of the survey (30 June / 31 December), YYYY-MM-DD",
    "target_period": "survey quarter, YYYYQ2 / YYYYQ4",
    "horizon": "0 only",
    "derived_value": "400*ln(RGDPX_ZM/RGDPX_BP), percent; derived from published median levels",
    "strict_start": LIVINGSTON_STRICT_START,
}


#: The NY Fed adapter's local archive schema (documented, validated row by row).
NYFED_SCHEMA = {
    "path": "data/raw/ny_fed/nowcast.csv (git-ignored)",
    "columns": ["date", "quarter", "nowcast", "provenance"],
    "date": "publication date of the nowcast, YYYY-MM-DD",
    "quarter": "target quarter of the nowcast, YYYYQn",
    "nowcast": "annualised quarter-on-quarter real GDP growth, percent",
    "provenance": "PUBLISHED | RECONSTRUCTED; anything else is rejected",
}

#: Reasons that mean "the information was not public at the origin" (counted as look-ahead).
LOOKAHEAD_REASONS = frozenset(
    {"LOOKAHEAD_REJECTED", "SPF_RELEASE_AFTER_ORIGIN", "NO_PUBLICATION_BEFORE_ORIGIN",
     "PUBLICATION_DATE_UNKNOWN", "LIVINGSTON_RELEASE_AFTER_ORIGIN"}
)

#: Register conditions that forbid publishing the source's values, even when the entry is OPEN.
PUBLICATION_BLOCKERS = (
    "never committed or published",
    "outside git",
    "values outside git",
    "never committed",
)


class ScorecardError(RuntimeError):
    """The scorecard cannot run (bad arguments, unreadable protocol/register)."""


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ScorecardError(f"invalid date {value!r}") from exc


def _iso(value) -> str | None:
    return None if value is None else _as_date(value).isoformat()


def _round(value, digits: int = 10):
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, digits)


def fmean(values: Sequence[float]) -> float:
    """Deterministic arithmetic mean (math.fsum, no numpy, fixed summation order)."""
    values = list(values)
    if not values:
        raise ScorecardError("fmean of an empty sequence")
    return math.fsum(values) / len(values)


def month_end(year: int, month: int) -> date:
    import calendar

    return date(year, month, calendar.monthrange(year, month)[1])


def is_month_end(value) -> bool:
    d = _as_date(value)
    return d == month_end(d.year, d.month)


def quarter_of(value) -> str:
    d = _as_date(value)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_end(value) -> str:
    d = _as_date(value)
    year, quarter = d.year, (d.month - 1) // 3 + 1
    return month_end(year, quarter * 3).isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relpath(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def _json_default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


# ----------------------------------------------------------------------------------------------
# Licence gate
# ----------------------------------------------------------------------------------------------
def load_licence_register(path: Path | str | None = None) -> dict:
    register_path = Path(path) if path is not None else LICENCE_PATH
    try:
        payload = json.loads(register_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScorecardError(f"cannot read licence register {register_path}: {exc}") from exc
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ScorecardError(f"licence register {register_path} has no entries list")
    return payload


def register_entry(register: Mapping, entry_id: str) -> dict | None:
    for entry in register.get("entries") or []:
        if entry.get("id") == entry_id:
            return dict(entry)
    return None


def licence_verdict(entry: Mapping | None) -> dict:
    """Usable for scoring?  Values publishable?  Derived only from the register entry.

    ``OPEN`` is necessary but not sufficient for publication: an OPEN entry may still carry a
    condition that keeps the values out of any published artefact (the SPF case).
    """
    if not entry:
        return {
            "status": "MISSING",
            "usable": False,
            "usage": "NOT_USED",
            "values_publishable": False,
            "publication_blocker": "NO_LICENCE_REGISTER_ENTRY",
            "reason": "NO_LICENCE_REGISTER_ENTRY",
            "conditions": [],
        }
    status = str(entry.get("status") or "UNVERIFIED").upper()
    conditions = [str(c) for c in (entry.get("conditions") or [])]
    blocker = next(
        (c for c in conditions if any(token in c.lower() for token in PUBLICATION_BLOCKERS)),
        None,
    )
    usable = status == "OPEN"
    publishable = bool(usable and blocker is None)
    return {
        "status": status,
        "level": entry.get("level"),
        "register_date": entry.get("date"),
        "last_reviewed": entry.get("last_reviewed"),
        "url": entry.get("url"),
        "conditions": conditions,
        "usable": usable,
        "usage": "SCORED" if usable else "NOT_USED",
        "values_publishable": publishable,
        "publication_blocker": blocker,
        "decision": entry.get("decision"),
        "reason": None if usable else f"LICENCE_{status}",
    }


def publication_block(records: Sequence[Mapping], verdict: Mapping, *, limit: int = 5,
                      allow_values: bool = True) -> dict:
    """Values block of one source.

    The licence verdict decides whether the *licence* would allow publishing a sample.  The report
    builder additionally applies the project policy (``REPORT_VALUES_POLICY``) and never publishes
    third-party values into the committed report: passing ``allow_values=False`` keeps the block
    aggregates-only even for an OPEN licence without publication blockers.
    """
    if not allow_values:
        return {
            "values_published": False,
            "reason": "PROJECT_VALUES_NOT_PUBLISHED",
            "licence_status": verdict.get("status"),
            "publication_blocker": None,
            "note": ("project policy: third-party forecast/actual values stay outside git; only "
                     "aggregates, counts, metadata and hashes are published"),
            "sample": None,
        }
    if not verdict.get("values_publishable"):
        return {
            "values_published": False,
            "reason": "LICENCE_VALUES_NOT_PUBLISHABLE",
            "licence_status": verdict.get("status"),
            "publication_blocker": (
                "a register condition keeps the values out of published artefacts"
                if verdict.get("publication_blocker") else verdict.get("reason") or "LICENCE_NOT_OPEN"
            ),
            "note": "only aggregates, counts and metadata are published for this source",
            "sample": None,
        }
    sample = [
        {
            "origin": record["origin"],
            "target_period": record.get("target_period"),
            "forecast": record.get("forecast"),
            "actual": record.get("actual"),
            "publication_date": record.get("publication_date"),
        }
        for record in records
        if record.get("status") == "SCORED"
    ][:limit]
    return {
        "values_published": True,
        "reason": None,
        "licence_status": verdict.get("status"),
        "note": f"licence allows publication; at most {limit} scored points are sampled",
        "sample": sample,
    }


# ----------------------------------------------------------------------------------------------
# PIT gates
# ----------------------------------------------------------------------------------------------
def pit_gate(publication_date, origin, *, before_reason: str = "LOOKAHEAD_REJECTED") -> tuple[bool, str]:
    """Information published on or before the origin may be used; later is look-ahead."""
    if publication_date is None:
        return False, "PUBLICATION_DATE_UNKNOWN"
    if _as_date(publication_date) > _as_date(origin):
        return False, before_reason
    return True, "OK"


def actual_pit_gate(actual: Mapping | None, origin) -> tuple[bool, str]:
    """The fact must be published strictly after the origin (otherwise it was already public)."""
    if not actual or actual.get("primary") is None:
        return False, "ACTUAL_NOT_AVAILABLE"
    vintage = actual.get("primary_vintage")
    if vintage is None:
        return False, "ACTUAL_VINTAGE_UNKNOWN"
    if _as_date(vintage) <= _as_date(origin):
        return False, "ACTUAL_PUBLISHED_AT_OR_ORIGIN"
    return True, "OK"


# ----------------------------------------------------------------------------------------------
# Losses
# ----------------------------------------------------------------------------------------------
PROTOCOL_QUANTILES = tuple(round(0.05 * k, 2) for k in range(1, 20))  # 5%..95% in 5% steps


def pinball_loss(actual: float, quantile: float, level: float) -> float:
    """Pinball (quantile) loss of ``quantile`` at probability level ``level`` for ``actual``."""
    delta = float(actual) - float(quantile)
    return max(level * delta, (level - 1.0) * delta)


def crps_from_quantiles(quantiles: Mapping[float, float], actual: float) -> float:
    """CRPS = twice the mean pinball loss over the 19 protocol quantiles.

    ``quantiles`` maps probability level -> quantile value.  The levels must be exactly the protocol
    grid and the quantile function must be non-decreasing; both are validated because a crossing
    quantile function makes the score meaningless (protocol ``forecast_product.distribution``).
    """
    if actual is None or (isinstance(actual, float) and not math.isfinite(actual)):
        raise ValueError("actual is not a finite number")
    levels = sorted(float(level) for level in quantiles)
    expected = list(PROTOCOL_QUANTILES)
    if len(levels) != len(expected) or any(abs(a - b) > 1e-9 for a, b in zip(levels, expected)):
        raise ValueError(f"quantile levels must be the protocol grid {expected}, got {levels}")
    values = [float(quantiles[level]) for level in levels]
    if any(not math.isfinite(v) for v in values):
        raise ValueError("non-finite quantile")
    if any(b < a for a, b in zip(values, values[1:])):
        raise ValueError("non-monotone quantile function (quantile crossing)")
    return 2.0 * fmean([pinball_loss(actual, value, level) for value, level in zip(values, levels)])


def brier_score(probability: float, outcome: float) -> float:
    """Brier score of a binary event probability."""
    p = float(probability)
    o = float(outcome)
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"probability outside [0, 1]: {p}")
    if o not in (0.0, 1.0):
        raise ValueError(f"event outcome must be 0 or 1, got {o}")
    return (p - o) ** 2


def covered_by_interval(quantiles: Mapping[float, float], actual: float) -> bool | None:
    """Is the fact inside the 5-95% interval?"""
    low, high = quantiles.get(0.05), quantiles.get(0.95)
    if low is None or high is None or actual is None:
        return None
    return float(low) <= float(actual) <= float(high)


def score_records(records: Sequence[Mapping]) -> dict:
    """Aggregate the losses of one (source, target, horizon, window) block."""
    scored = [dict(r) for r in records if r.get("status") == "SCORED"]
    metrics: dict[str, float | None] = {"rmse": None, "mae": None, "bias": None,
                                        "crps": None, "brier": None, "coverage_90": None}
    status = {key: "NO_SCORED_POINTS" for key in metrics}
    out: dict = {"n": len(scored), "metrics": metrics, "metric_status": status,
                 "secondary": {"n": 0, "rmse": None, "mae": None}}
    if not scored:
        return out
    errors = [float(r["forecast"]) - float(r["actual"]) for r in scored]
    metrics["bias"] = _round(fmean(errors))
    metrics["mae"] = _round(fmean([abs(e) for e in errors]))
    metrics["rmse"] = _round(math.sqrt(fmean([e * e for e in errors])))
    for key in ("bias", "mae", "rmse"):
        status[key] = "AVAILABLE"

    # CRPS and interval coverage need a predictive distribution.
    distributed = [r for r in scored if r.get("quantiles")]
    if not distributed:
        status["crps"] = "NO_PREDICTIVE_DISTRIBUTION"
        status["coverage_90"] = "NO_PREDICTIVE_DISTRIBUTION"
    else:
        try:
            crps_values = [crps_from_quantiles(r["quantiles"], r["actual"]) for r in distributed]
        except ValueError as exc:
            status["crps"] = f"INVALID_QUANTILES: {exc}"
            status["coverage_90"] = status["crps"]
        else:
            metrics["crps"] = _round(fmean(crps_values))
            status["crps"] = "AVAILABLE" if len(distributed) == len(scored) else "AVAILABLE_PARTIAL_SAMPLE"
            covered = [covered_by_interval(r["quantiles"], r["actual"]) for r in distributed]
            known = [c for c in covered if c is not None]
            if known:
                metrics["coverage_90"] = _round(fmean([1.0 if c else 0.0 for c in known]))
                status["coverage_90"] = "AVAILABLE" if len(known) == len(scored) else "AVAILABLE_PARTIAL_SAMPLE"
            else:
                status["coverage_90"] = "INTERVAL_NOT_AVAILABLE"
        out["distribution_points"] = len(distributed)

    # Brier needs an event probability and a resolved event outcome.
    probabilistic = [r for r in scored if r.get("probability") is not None]
    resolved = [r for r in probabilistic if r.get("outcome") is not None]
    if not probabilistic:
        status["brier"] = "NO_EVENT_PROBABILITY_MAPPING"
    elif not resolved:
        status["brier"] = "EVENT_OUTCOME_NOT_AVAILABLE"
    else:
        try:
            metrics["brier"] = _round(fmean([brier_score(r["probability"], r["outcome"]) for r in resolved]))
        except ValueError as exc:
            status["brier"] = f"INVALID_EVENT_INPUT: {exc}"
        else:
            status["brier"] = "AVAILABLE" if len(resolved) == len(scored) else "AVAILABLE_PARTIAL_SAMPLE"
            out["event_points"] = len(resolved)

    # Secondary actuals (12-month vintage): reported separately, never pooled with the primary.
    secondary = [r for r in scored if r.get("secondary_actual") is not None]
    if secondary:
        errors2 = [float(r["forecast"]) - float(r["secondary_actual"]) for r in secondary]
        out["secondary"] = {
            "n": len(secondary),
            "rmse": _round(math.sqrt(fmean([e * e for e in errors2]))),
            "mae": _round(fmean([abs(e) for e in errors2])),
            "rmse_delta_vs_primary": _round(
                math.sqrt(fmean([e * e for e in errors2])) - metrics["rmse"]
            ) if metrics["rmse"] is not None else None,
            "rule": "protocol actuals.secondary, value in the vintage 12 months after the period",
        }
    else:
        out["secondary"] = {"n": 0, "rmse": None, "mae": None,
                            "reason": "SECONDARY_ACTUALS_NOT_AVAILABLE"}
    return out


# ----------------------------------------------------------------------------------------------
# Source metadata
# ----------------------------------------------------------------------------------------------
SOURCE_SPECS: dict[str, dict] = {
    "SPF_GDP_MEDIAN": {
        "name": "Survey of Professional Forecasters - median real GDP growth",
        "provider": "Federal Reserve Bank of Philadelphia",
        "licence_entry": "PHILADELPHIA_FED",
        "adapter": "spf_local",
        "urls": [
            "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/survey-of-professional-forecasters",
            "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts",
        ],
        "values_location": "data/raw/spf/spf_median.json (git-ignored)",
        "attribution": SPF_ATTRIBUTION,
        "supported_targets": {TARGET_T2: [0, 1]},
        "target_mapping": {
            TARGET_T2: {str(h): code for h, code in SPF_GDP_CODE_FOR_HORIZON.items()},
        },
        "publication_rule": (
            "official SPF news-release date of the survey quarter (spf-release-dates.txt; "
            "spf_calendar.release_date), never the response deadline; no real-time data before 1990Q3"
        ),
        "pit_notes": [
            "the median is usable only at a month-end origin on or after the news-release date",
            "h=0 requires the origin to be the month end of the survey quarter's middle month",
            "h>0 uses the quarter-end origin whose survey quarter was already released in its middle month",
            "the file is the latest revision of the medians, not a point-in-time vintage snapshot",
        ],
        "distribution": None,
        "event_probability": None,
    },
    "NY_FED_STAFF_NOWCAST": {
        "name": "Staff Nowcast - current-quarter real GDP growth",
        "provider": "Federal Reserve Bank of New York",
        "licence_entry": "NY_FED",
        "adapter": "ny_fed_local",
        "urls": [
            "https://www.newyorkfed.org/research/policy/nowcast",
            "https://www.newyorkfed.org/privacy/termsofuse",
            "https://www.newyorkfed.org/medialibrary/media/research/policy/nowcast/"
            "new-york-fed-staff-nowcast_data_2002-present.xlsx",
        ],
        "values_location": NYFED_SCHEMA["path"],
        "attribution": NYFED_ATTRIBUTION,
        "supported_targets": {TARGET_T2: [0]},
        "target_mapping": {TARGET_T2: {"0": "nowcast of the current quarter"}},
        "publication_rule": (
            "publication date column of the local archive; only rows published on or before the "
            "origin are used, and rows flagged RECONSTRUCTED are not real-time publications"
        ),
        "pit_notes": [
            "the archive is values-only: it carries no underlying series, so no re-estimation is possible",
            "2002Q1-2015Q4 rows in the public archive are historical reconstructions, not publications",
            "updates were suspended from September 2021 to September 2023 (licence register note)",
        ],
        "distribution": None,
        "event_probability": None,
    },
    "LIVINGSTON_SURVEY": {
        "name": "Livingston Survey - median real GDP level, derived q/q growth",
        "provider": "Federal Reserve Bank of Philadelphia",
        "licence_entry": "PHILADELPHIA_FED",
        "adapter": "livingston_local",
        "urls": [
            "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/livingston-survey",
            "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/livingston-historical-data",
            "https://www.philadelphiafed.org/about-us/privacy-notice",
        ],
        "values_location": LIVINGSTON_SCHEMA["path"],
        "attribution": LIVINGSTON_ATTRIBUTION,
        "supported_targets": {TARGET_T2: [0]},
        "target_mapping": {
            TARGET_T2: {"0": "400*ln(RGDPX_ZM/RGDPX_BP) from published median levels; "
                            "strict post-2004 subset only"},
        },
        "publication_rule": (
            "official Livingston release date from livingston-release-dates.xlsx: Exact if present, "
            "else Not After as a PIT-safe upper bound; only rows with release_date <= origin are used"
        ),
        "pit_notes": [
            "survey is semiannual (June/December): only the 30 June / 31 December quarter-end origins "
            "are origin-compatible with h=0; other month ends are excluded, never interpolated",
            "strict subset starts 2004-06: the survey documentation warns that X_BP before June 2004 "
            "is unreliable for variables other than CPI",
            "forecast is a documented transformation of published median levels, not a separately "
            "published median growth forecast; no reconstruction or interpolation is applied",
            "release dates before 2001 are 'Not After' upper bounds where no Exact date is published",
        ],
        "distribution": None,
        "event_probability": None,
    },
    "CLEVELAND_FED_NOWCAST": {
        "name": "Inflation Nowcasting - current-month CPI",
        "provider": "Federal Reserve Bank of Cleveland",
        "licence_entry": "CLEVELAND_FED",
        "adapter": "metadata_only",
        "urls": [
            "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting",
            "https://www.clevelandfed.org/privacy",
        ],
        "values_location": None,
        "attribution": CLEVELAND_ATTRIBUTION,
        "supported_targets": {},
        "target_mapping": {},
        "publication_rule": "daily nowcast releases; no local archive in this repository",
        "pit_notes": [
            "the published figure is a current-month CPI nowcast, while the protocol T1 target is "
            "year-over-year inflation at t-1+h: there is no horizon mapping, so nothing can be scored",
        ],
        "distribution": None,
        "event_probability": None,
    },
    "ATLANTA_FED_GDPNOW": {
        "name": "GDPNow - current-quarter real GDP growth",
        "provider": "Federal Reserve Bank of Atlanta",
        "licence_entry": "ATLANTA_FED_GDPNOW",
        "adapter": "restricted",
        "urls": [
            "https://www.atlantafed.org/research-and-data/data/gdpnow",
            "https://www.atlantafed.org/terms-of-use",
        ],
        "values_location": None,
        "attribution": GDPNOW_ATTRIBUTION,
        "supported_targets": {},
        "target_mapping": {},
        "publication_rule": "not used: the register entry is RESTRICTED",
        "pit_notes": [
            "values are never read, even when a local copy exists",
            "the model quantity is replaced by the project's own OPEN OWN_NOWCAST_BRIDGE "
            "(protocol licence_policy.substitute); the substitute is never presented as equal",
        ],
        "distribution": None,
        "event_probability": None,
    },
}


# ----------------------------------------------------------------------------------------------
# Adapters
# ----------------------------------------------------------------------------------------------
class Adapter:
    """Minimal adapter protocol: metadata, status, one forecast per (target, origin, horizon)."""

    source_id = "ABSTRACT"

    def __init__(self, *, data_root: Path | str | None = None, register: Mapping | None = None,
                 table: Mapping | None = None) -> None:
        self.data_root = Path(data_root) if data_root is not None else DEFAULT_DATA_ROOT
        self.register = register if register is not None else load_licence_register()
        self.table_override = table
        self.spec = dict(SOURCE_SPECS[self.source_id])
        self.entry = register_entry(self.register, self.spec["licence_entry"])
        self.verdict = licence_verdict(self.entry)
        self.values_path: Path | None = None
        self.load_error: str | None = None

    # -- introspection -------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "source_id": self.source_id,
            "name": self.spec["name"],
            "provider": self.spec["provider"],
            "urls": list(self.spec["urls"]),
            "attribution": self.spec["attribution"],
            "licence_entry": self.spec["licence_entry"],
            "values_location": self.spec["values_location"],
            "publication_rule": self.spec["publication_rule"],
            "pit_notes": list(self.spec["pit_notes"]),
            "supported_targets": {k: sorted(v) for k, v in self.spec["supported_targets"].items()},
            "target_mapping": self.spec["target_mapping"],
            "distribution": self.spec["distribution"],
            "event_probability": self.spec["event_probability"],
        }

    def metadata(self) -> dict:
        describe = self.describe()
        describe["licence"] = self.verdict
        describe["adapter"] = self.spec["adapter"]
        return describe

    def status(self) -> dict:
        if not self.verdict["usable"]:
            return {
                "status": "RESTRICTED_NOT_USED",
                "reason": self.verdict.get("reason") or "LICENCE_NOT_OPEN",
                "detail": "the licence register entry does not allow use; values are never read",
                "available": False,
            }
        if not self.spec["supported_targets"]:
            return {
                "status": "NO_PROTOCOL_HORIZON_MAPPING",
                "reason": "NO_PROTOCOL_HORIZON_MAPPING",
                "detail": "the published forecast cannot be mapped to a protocol target/horizon",
                "available": False,
            }
        return {"status": "AVAILABLE", "reason": None, "detail": None, "available": True}

    def forecast(self, target: str, origin, horizon: int) -> dict:
        raise NotImplementedError

    # -- shared helpers ------------------------------------------------------------------
    def _base(self, target: str, origin, horizon: int) -> dict:
        return {
            "source": self.source_id,
            "target": target,
            "origin": _iso(origin),
            "horizon": int(horizon),
            "available": False,
            "reason": None,
            "publication_date": None,
            "forecast": None,
            "quantiles": None,
            "probability": None,
            "provenance": None,
        }

    def _licence_refusal(self, base: dict) -> dict:
        base["reason"] = self.verdict.get("reason") or "LICENCE_NOT_OPEN"
        return base


class MetadataOnlyAdapter(Adapter):
    """A source with a readable licence but no mappable protocol target (or no archive)."""

    source_id = "CLEVELAND_FED_NOWCAST"

    def status(self) -> dict:
        if not self.verdict["usable"]:
            return {
                "status": "RESTRICTED_NOT_USED",
                "reason": self.verdict.get("reason") or "LICENCE_NOT_OPEN",
                "detail": "the licence register entry does not allow use; values are never read",
                "available": False,
            }
        return {
            "status": "UNAVAILABLE",
            "reason": "NO_PROTOCOL_HORIZON_MAPPING",
            "detail": "published nowcast is a current-month CPI figure; the protocol T1 target is y/y at t-1+h",
            "available": False,
        }

    def forecast(self, target: str, origin, horizon: int) -> dict:
        base = self._base(target, origin, horizon)
        base["reason"] = "NO_PROTOCOL_HORIZON_MAPPING"
        return base


class RestrictedAdapter(Adapter):
    """A RESTRICTED source: metadata only, values are never read."""

    source_id = "ATLANTA_FED_GDPNOW"

    def status(self) -> dict:
        return {
            "status": "RESTRICTED_NOT_USED",
            "reason": self.verdict.get("reason") or "LICENCE_RESTRICTED",
            "detail": "RESTRICTED register entry: values are not read; OWN_NOWCAST_BRIDGE is the OPEN substitute",
            "available": False,
        }

    def forecast(self, target: str, origin, horizon: int) -> dict:
        base = self._base(target, origin, horizon)
        base["reason"] = self.verdict.get("reason") or "LICENCE_RESTRICTED"
        return base


class SpfAdapter(Adapter):
    """SPF median GDP growth from the git-ignored local file (release-date availability)."""

    source_id = "SPF_GDP_MEDIAN"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.table: dict[str, dict[str, float]] | None = None
        self.observations = 0
        self._load()

    # -- data ---------------------------------------------------------------------------
    def _load(self) -> None:
        if self.table_override is not None:
            self.table = {str(code): {str(q): float(v) for q, v in rows.items()}
                          for code, rows in dict(self.table_override).items()}
            self.observations = sum(len(rows) for rows in self.table.values())
            self.values_path = None
            return
        path = self.data_root / "spf" / "spf_median.json"
        self.values_path = path
        if not path.exists():
            self.load_error = "SPF_VALUES_NOT_DOWNLOADED"
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.load_error = "SPF_VALUES_UNREADABLE"
            return
        rows = payload.get("series") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list) or not rows:
            self.load_error = "SPF_VALUES_UNREADABLE"
            return
        table: dict[str, dict[str, float]] = {}
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            code, quarter, raw = row.get("code"), row.get("survey_quarter"), row.get("value")
            if code not in SPF_GDP_CODES or not isinstance(quarter, str):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            key = (str(code), quarter)
            if key in seen:
                self.load_error = "SPF_DUPLICATE_OBSERVATION"
                self.table = None
                return
            seen.add(key)
            table.setdefault(str(code), {})[quarter] = value
        if not table:
            self.load_error = "SPF_VALUES_UNREADABLE"
            return
        self.table = table
        self.observations = len(seen)

    # -- introspection -------------------------------------------------------------------
    def status(self) -> dict:
        if not self.verdict["usable"]:
            return {
                "status": "RESTRICTED_NOT_USED",
                "reason": self.verdict.get("reason") or "LICENCE_NOT_OPEN",
                "detail": "the licence register entry does not allow use; values are never read",
                "available": False,
            }
        if self.table is None:
            return {
                "status": "UNAVAILABLE",
                "reason": self.load_error or "SPF_VALUES_NOT_DOWNLOADED",
                "detail": (
                    "the SPF median values are third-party data and stay in the git-ignored data "
                    "directory; run tools/fetch_spf.py or restore the private r2-metadata artifact"
                ),
                "available": False,
            }
        return {"status": "AVAILABLE", "reason": None, "detail": None, "available": True}

    def metadata(self) -> dict:
        out = super().metadata()
        file_block: dict = {"values_present": self.table is not None}
        if self.values_path is not None:
            file_block["path"] = _relpath(self.values_path)
            if self.values_path.exists():
                file_block["sha256"] = sha256_file(self.values_path)
        if self.table is not None:
            file_block["observations"] = self.observations
            file_block["codes"] = {
                code: {"quarters": len(rows), "first": min(rows), "last": max(rows)}
                for code, rows in sorted(self.table.items())
            }
        out["values_file"] = file_block
        return out

    # -- availability --------------------------------------------------------------------
    def survey_quarter(self, origin, horizon: int) -> tuple[str | None, str]:
        """Official SPF survey quarter that supplies the forecast at ``origin``."""
        d = _as_date(origin)
        if not is_month_end(d):
            return None, "ORIGIN_NOT_MONTH_END"
        if horizon == 0:
            if d.month not in SPF_MIDDLE_MONTHS:
                return None, "ORIGIN_NOT_A_SURVEY_MONTH"
            return f"{d.year}Q{(d.month + 1) // 3}", "OK"
        # h>0: the origin is the quarter end; that quarter's survey was released in its middle month.
        return quarter_of(d), "OK"

    def forecast(self, target: str, origin, horizon: int) -> dict:
        base = self._base(target, origin, horizon)
        if not self.verdict["usable"]:
            return self._licence_refusal(base)
        if target != TARGET_T2:
            base["reason"] = "UNSUPPORTED_TARGET"
            return base
        code = SPF_GDP_CODE_FOR_HORIZON.get(int(horizon))
        if code is None:
            base["reason"] = f"NO_SPF_HORIZON_CODE_FOR_H{int(horizon)}"
            return base
        base["code"] = code
        if self.table is None:
            base["reason"] = self.load_error or "SPF_VALUES_NOT_DOWNLOADED"
            return base
        survey_quarter, why = self.survey_quarter(origin, int(horizon))
        if survey_quarter is None:
            base["reason"] = why
            return base
        base["survey_quarter"] = survey_quarter
        if not spf_is_real_time_quarter(survey_quarter):
            base["reason"] = "SPF_NOT_REAL_TIME_BEFORE_1990Q3"
            return base
        publication = spf_release_date(survey_quarter)
        base["publication_date"] = publication.isoformat()
        ok, why = pit_gate(publication, origin, before_reason="SPF_RELEASE_AFTER_ORIGIN")
        if not ok:
            base["reason"] = why
            return base
        value = self.table.get(code, {}).get(survey_quarter)
        if value is None:
            base["reason"] = "SURVEY_QUARTER_NOT_IN_FILE"
            return base
        base.update({"available": True, "reason": "AVAILABLE", "forecast": float(value),
                     "provenance": "PUBLISHED"})
        return base


class NyFedAdapter(Adapter):
    """NY Fed Staff Nowcast from an optional local archive with a strict, documented schema."""

    source_id = "NY_FED_STAFF_NOWCAST"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: list[dict] | None = None
        self.row_errors: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if self.table_override is not None:
            self.rows = [dict(row) for row in self.table_override]
            return
        path = self.data_root / "ny_fed" / "nowcast.csv"
        self.values_path = path
        if not path.exists():
            self.load_error = "ARCHIVE_NOT_LOCAL"
            return
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:
            self.load_error = "ARCHIVE_UNREADABLE"
            return
        parsed: list[dict] = []
        errors: dict[str, int] = {}
        for raw in rows:
            row, why = self._parse_row(raw)
            if row is None:
                errors[why] = errors.get(why, 0) + 1
                continue
            parsed.append(row)
        self.rows = parsed
        self.row_errors = errors
        if not parsed:
            self.load_error = "ARCHIVE_HAS_NO_USABLE_ROWS"

    @staticmethod
    def _parse_row(raw: Mapping) -> tuple[dict | None, str]:
        try:
            published = _as_date(raw.get("date"))
            quarter = str(raw.get("quarter") or "").strip().upper()
            value = float(raw.get("nowcast"))
        except (TypeError, ValueError, ScorecardError):
            return None, "ROW_UNPARSEABLE"
        if not math.isfinite(value):
            return None, "ROW_UNPARSEABLE"
        if len(quarter) != 6 or quarter[4] != "Q" or not quarter[:4].isdigit() or quarter[5] not in "1234":
            return None, "ROW_BAD_QUARTER"
        provenance = str(raw.get("provenance") or "").strip().upper()
        if provenance not in ("PUBLISHED", "RECONSTRUCTED"):
            return None, "ROW_PROVENANCE_UNKNOWN"
        return {"date": published, "quarter": quarter, "nowcast": value,
                "provenance": provenance}, "OK"

    def status(self) -> dict:
        if not self.verdict["usable"]:
            return {
                "status": "RESTRICTED_NOT_USED",
                "reason": self.verdict.get("reason") or "LICENCE_NOT_OPEN",
                "detail": "the licence register entry does not allow use; values are never read",
                "available": False,
            }
        if self.rows is None or not self.rows:
            return {
                "status": "UNAVAILABLE",
                "reason": self.load_error or "ARCHIVE_NOT_LOCAL",
                "detail": f"optional local archive not usable; expected schema {NYFED_SCHEMA}",
                "available": False,
            }
        return {"status": "AVAILABLE", "reason": None,
                "detail": f"{len(self.rows)} archived nowcasts", "available": True}

    def metadata(self) -> dict:
        out = super().metadata()
        block: dict = {
            "schema": NYFED_SCHEMA,
            "values_present": bool(self.rows),
            "archive_path": _relpath(self.values_path) if self.values_path is not None else None,
            "rows": len(self.rows or []),
            "row_errors": dict(sorted(self.row_errors.items())),
            "values_only_archive": True,
        }
        if self.values_path is not None and self.values_path.exists():
            block["sha256"] = sha256_file(self.values_path)
        out["values_file"] = block
        return out

    def forecast(self, target: str, origin, horizon: int) -> dict:
        base = self._base(target, origin, horizon)
        if not self.verdict["usable"]:
            return self._licence_refusal(base)
        if target != TARGET_T2 or int(horizon) != 0:
            base["reason"] = "NO_PROTOCOL_HORIZON_MAPPING"
            return base
        d = _as_date(origin)
        if not is_month_end(d):
            base["reason"] = "ORIGIN_NOT_MONTH_END"
            return base
        if not self.rows:
            base["reason"] = self.load_error or "ARCHIVE_NOT_LOCAL"
            return base
        quarter = quarter_of(d)
        candidates = [row for row in self.rows if row["quarter"] == quarter and row["date"] <= d]
        if not candidates:
            base["reason"] = "NO_PUBLICATION_BEFORE_ORIGIN"
            return base
        latest = max(candidates, key=lambda row: row["date"])
        base["publication_date"] = latest["date"].isoformat()
        if latest["provenance"] != "PUBLISHED":
            base["reason"] = "RECONSTRUCTED_NOT_REAL_TIME"
            base["provenance"] = latest["provenance"]
            return base
        base.update({"available": True, "reason": "AVAILABLE", "forecast": float(latest["nowcast"]),
                     "provenance": "PUBLISHED"})
        return base


class LivingstonAdapter(Adapter):
    """Livingston Survey median levels -> protocol T2 h=0 q/q annualised growth (strict post-2004).

    The local CSV is derived by ``tools/fetch_scorecard_data.py --livingston`` from the published
    ``medians.xlsx`` (sheet RGDPX: RGDPX_BP, RGDPX_ZM) and ``livingston-release-dates.xlsx``
    (sheet Dates: Exact / Not After).  ``forecast = 400*ln(RGDPX_ZM/RGDPX_BP)`` is the protocol T2
    definition applied to published median levels, a documented transformation rather than a
    separately published median growth forecast.  Only the 30 June / 31 December quarter-end origins
    are used; no interpolation is applied to other month ends.
    """

    source_id = "LIVINGSTON_SURVEY"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: list[dict] | None = None
        self.row_errors: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if self.table_override is not None:
            # Test/derivation override: normalise the documented keys so forecast() can use them.
            normalised: list[dict] = []
            for raw in self.table_override:
                row = dict(raw)
                try:
                    row["survey_date"] = _as_date(row["survey_date"])
                    row["release_date"] = _as_date(row["release_date"])
                    row["origin"] = _as_date(row["origin"])
                    row["target_period"] = str(row["target_period"]).strip().upper()
                    row["horizon"] = int(row["horizon"])
                    row["forecast"] = float(row["forecast"])
                except (KeyError, TypeError, ValueError, ScorecardError):
                    self.row_errors["ROW_UNPARSEABLE"] = self.row_errors.get("ROW_UNPARSEABLE", 0) + 1
                    continue
                row["release_basis"] = str(row.get("release_basis") or "").strip().lower()
                normalised.append(row)
            self.rows = normalised
            if not normalised:
                self.load_error = "LIVINGSTON_ARCHIVE_HAS_NO_USABLE_ROWS"
            return
        path = self.data_root / "livingston" / "livingston_t2_h0.csv"
        self.values_path = path
        if not path.exists():
            self.load_error = "LIVINGSTON_VALUES_NOT_DOWNLOADED"
            return
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                raw_rows = list(csv.DictReader(handle))
        except OSError:
            self.load_error = "LIVINGSTON_ARCHIVE_UNREADABLE"
            return
        parsed: list[dict] = []
        errors: dict[str, int] = {}
        seen_origins: set[str] = set()
        for raw in raw_rows:
            row, why = self._parse_row(raw)
            if row is None:
                errors[why] = errors.get(why, 0) + 1
                continue
            if row["origin"].isoformat() in seen_origins:
                errors["ROW_DUPLICATE_ORIGIN"] = errors.get("ROW_DUPLICATE_ORIGIN", 0) + 1
                continue
            seen_origins.add(row["origin"].isoformat())
            parsed.append(row)
        parsed.sort(key=lambda item: item["origin"])
        self.rows = parsed
        self.row_errors = errors
        if not parsed:
            self.load_error = "LIVINGSTON_ARCHIVE_HAS_NO_USABLE_ROWS"

    @staticmethod
    def _parse_row(raw: Mapping) -> tuple[dict | None, str]:
        try:
            survey = _as_date(raw.get("survey_date"))
            release = _as_date(raw.get("release_date"))
            origin = _as_date(raw.get("origin"))
            horizon = int(str(raw.get("horizon", "")).strip())
            forecast = float(str(raw.get("forecast", "")).strip())
        except (TypeError, ValueError, ScorecardError):
            return None, "ROW_UNPARSEABLE"
        if not math.isfinite(forecast):
            return None, "ROW_UNPARSEABLE"
        basis = str(raw.get("release_basis") or "").strip().lower()
        if basis not in ("exact", "not_after"):
            return None, "ROW_RELEASE_BASIS_UNKNOWN"
        if horizon != 0:
            return None, "ROW_HORIZON_NON_ZERO"
        target = str(raw.get("target_period") or "").strip().upper()
        if (len(target) != 6 or target[4] != "Q" or not target[:4].isdigit()
                or target[5] not in "24"):
            return None, "ROW_BAD_TARGET_PERIOD"
        if quarter_of(survey) != target:
            return None, "ROW_TARGET_NOT_SURVEY_QUARTER"
        if origin != _as_date(quarter_end(survey)):
            return None, "ROW_ORIGIN_NOT_SURVEY_QUARTER_END"
        if origin.month not in (6, 12) or origin != month_end(origin.year, origin.month):
            return None, "ROW_ORIGIN_NOT_JUNE_DECEMBER_QUARTER_END"
        if release > origin:
            return None, "ROW_RELEASE_AFTER_ORIGIN"
        if survey < _as_date(LIVINGSTON_STRICT_START):
            return None, "ROW_PRE_2004_BASE_VALUE_CAVEAT"
        return {"survey_date": survey, "release_date": release, "release_basis": basis,
                "origin": origin, "target_period": target, "horizon": horizon,
                "forecast": forecast}, "OK"

    def status(self) -> dict:
        if not self.verdict["usable"]:
            return {
                "status": "RESTRICTED_NOT_USED",
                "reason": self.verdict.get("reason") or "LICENCE_NOT_OPEN",
                "detail": "the licence register entry does not allow use; values are never read",
                "available": False,
            }
        if self.rows is None or not self.rows:
            return {
                "status": "UNAVAILABLE",
                "reason": self.load_error or "LIVINGSTON_VALUES_NOT_DOWNLOADED",
                "detail": f"optional local archive not usable; expected schema {LIVINGSTON_SCHEMA}",
                "available": False,
            }
        return {"status": "AVAILABLE", "reason": None,
                "detail": f"{len(self.rows)} strict Livingston PIT rows", "available": True}

    def metadata(self) -> dict:
        out = super().metadata()
        rows = self.rows or []
        block: dict = {
            "schema": LIVINGSTON_SCHEMA,
            "values_present": bool(rows),
            "archive_path": _relpath(self.values_path) if self.values_path is not None else None,
            "rows": len(rows),
            "row_errors": dict(sorted(self.row_errors.items())),
            "strict_start": LIVINGSTON_STRICT_START,
            "release_basis": {
                "exact": sum(1 for row in rows if row["release_basis"] == "exact"),
                "not_after": sum(1 for row in rows if row["release_basis"] == "not_after"),
            },
            "transformation": "400*ln(RGDPX_ZM/RGDPX_BP) from published median levels",
            "values_only_archive": False,
        }
        if self.values_path is not None and self.values_path.exists():
            block["sha256"] = sha256_file(self.values_path)
        out["values_file"] = block
        return out

    def forecast(self, target: str, origin, horizon: int) -> dict:
        base = self._base(target, origin, horizon)
        if not self.verdict["usable"]:
            return self._licence_refusal(base)
        if target != TARGET_T2 or int(horizon) != 0:
            base["reason"] = "NO_PROTOCOL_HORIZON_MAPPING"
            return base
        d = _as_date(origin)
        if not is_month_end(d):
            base["reason"] = "ORIGIN_NOT_MONTH_END"
            return base
        if d.month not in (6, 12):
            base["reason"] = "LIVINGSTON_NO_QUARTER_END_ORIGIN"
            return base
        if d < _as_date(LIVINGSTON_STRICT_START):
            base["reason"] = "LIVINGSTON_BASE_VALUE_CAVEAT"
            return base
        if not self.rows:
            base["reason"] = self.load_error or "LIVINGSTON_VALUES_NOT_DOWNLOADED"
            return base
        row = next((item for item in self.rows if item["origin"] == d), None)
        if row is None:
            base["reason"] = "LIVINGSTON_NO_SURVEY_FOR_ORIGIN"
            return base
        base["publication_date"] = row["release_date"].isoformat()
        if row["release_date"] > d:
            base["reason"] = "LIVINGSTON_RELEASE_AFTER_ORIGIN"
            return base
        base.update({"available": True, "reason": "AVAILABLE",
                     "forecast": float(row["forecast"]),
                     "provenance": "PUBLISHED_MEDIAN_LEVELS_DERIVED_GROWTH"})
        return base


ADAPTERS: dict[str, type[Adapter]] = {
    "SPF_GDP_MEDIAN": SpfAdapter,
    "NY_FED_STAFF_NOWCAST": NyFedAdapter,
    "LIVINGSTON_SURVEY": LivingstonAdapter,
    "CLEVELAND_FED_NOWCAST": MetadataOnlyAdapter,
    "ATLANTA_FED_GDPNOW": RestrictedAdapter,
}


def build_adapters(*, data_root=None, register: Mapping | None = None) -> dict[str, Adapter]:
    register = register if register is not None else load_licence_register()
    return {source_id: cls(data_root=data_root, register=register)
            for source_id, cls in ADAPTERS.items()}


# ----------------------------------------------------------------------------------------------
# Scoring driver
# ----------------------------------------------------------------------------------------------
class ActualCache:
    """Memoised first-publication actuals (one vintage load per origin at most)."""

    def __init__(self, *, data_root=None) -> None:
        self.data_root = data_root
        self._cache: dict[tuple[str, str, int], dict | None] = {}
        self.calls = 0

    def __call__(self, target: str, origin, horizon: int) -> dict | None:
        key = (target, str(origin), int(horizon))
        if key not in self._cache:
            self.calls += 1
            try:
                self._cache[key] = actual_pair(target, origin, int(horizon), data_root=self.data_root)
            except (OSError, ValueError):
                self._cache[key] = None
        return self._cache[key]


def _score_window(adapter: Adapter, protocol: Mapping, target: str, horizon: int, window: str,
                  *, actuals: "ActualCache | None", max_origins: int | None = None) -> dict:
    """Score one (target, horizon, window) block.

    ``actuals is None`` is the metadata/availability mode: the publication gate is still applied but
    no fact is loaded, so the block reports ``available`` points instead of ``scored`` ones.
    """
    origins = windows_module.origins_for(dict(protocol), target, int(horizon), window)
    if max_origins is not None:
        origins = origins[: int(max_origins)]
    records: list[dict] = []
    exclusions: dict[str, int] = {}
    available = 0
    for row in origins:
        origin = row["origin"]
        forecast = adapter.forecast(target, origin, int(horizon))
        if not forecast.get("available"):
            reason = forecast.get("reason") or "FORECAST_UNAVAILABLE"
            exclusions[reason] = exclusions.get(reason, 0) + 1
            records.append({"origin": origin, "status": "EXCLUDED", "reason": reason})
            continue
        ok, why = pit_gate(forecast.get("publication_date"), origin,
                           before_reason="SPF_RELEASE_AFTER_ORIGIN"
                           if adapter.source_id == "SPF_GDP_MEDIAN" else "LOOKAHEAD_REJECTED")
        if not ok:
            exclusions[why] = exclusions.get(why, 0) + 1
            records.append({"origin": origin, "status": "EXCLUDED", "reason": why,
                            "publication_date": forecast.get("publication_date")})
            continue
        if actuals is None:
            available += 1
            records.append({"origin": origin, "status": "AVAILABLE",
                            "publication_date": forecast.get("publication_date"),
                            "forecast": forecast.get("forecast")})
            continue
        actual = actuals(target, origin, int(horizon))
        ok_a, why_a = actual_pit_gate(actual, origin)
        if not ok_a:
            exclusions[why_a] = exclusions.get(why_a, 0) + 1
            records.append({"origin": origin, "status": "EXCLUDED", "reason": why_a,
                            "publication_date": forecast.get("publication_date")})
            continue
        records.append({
            "origin": origin,
            "target_period": actual.get("period_end") or row.get("target"),
            "status": "SCORED",
            "forecast": float(forecast["forecast"]),
            "actual": float(actual["primary"]),
            "secondary_actual": actual.get("secondary"),
            "publication_date": forecast.get("publication_date"),
            "actual_vintage": actual.get("primary_vintage"),
            "secondary_vintage": actual.get("secondary_vintage"),
            "provenance": forecast.get("provenance"),
            "quantiles": forecast.get("quantiles"),
            "probability": forecast.get("probability"),
            "outcome": forecast.get("outcome"),
        })
    scored = [r for r in records if r["status"] == "SCORED"]
    block = {
        "window": window,
        "origins_eligible": len(origins),
        "available": available if actuals is None else len(scored),
        "scored": len(scored),
        "coverage_share": _round(len(scored) / len(origins)) if origins and actuals is not None
        else (_round(available / len(origins)) if origins else None),
        "lookahead_excluded": sum(count for reason, count in exclusions.items()
                                  if reason in LOOKAHEAD_REASONS),
        "exclusions": dict(sorted(exclusions.items())),
    }
    block.update(score_records(scored))
    block["_records"] = records  # dropped before the report is written
    return block


def _merge_windows(blocks: Mapping[str, Mapping], key: str) -> list:
    return [record for block in blocks.values() for record in block.get(key, [])]


def _score_source(adapter: Adapter, protocol: Mapping, *, horizons: Sequence[int],
                  actuals: "ActualCache | None", max_origins: int | None = None) -> dict:
    """All scored blocks of one source, with counts, publication policy and a values block."""
    result: dict = {
        "metadata": adapter.metadata(),
        "status": adapter.status(),
        "targets": {},
        "points": {"scored": 0, "available": 0, "eligible": 0, "by_target": {}},
        "counts_mode": "scored" if actuals is not None else "available",
    }
    if not adapter.status()["available"]:
        empty = publication_block([], adapter.verdict, allow_values=False)
        empty["values_published"] = False
        if not empty.get("reason"):
            empty["reason"] = "SOURCE_NOT_SCORED"
            empty["sample"] = None
        result["values"] = empty
        return result
    published_records: list[dict] = []
    for target, horizon_list in adapter.spec["supported_targets"].items():
        target_block: dict = {}
        for horizon in sorted(int(h) for h in horizon_list if int(h) in set(int(x) for x in horizons)):
            windows: dict = {}
            for window in ("W1", "W2"):
                windows[window] = _score_window(adapter, protocol, target, horizon, window,
                                                actuals=actuals, max_origins=max_origins)
            combined_records = [r for window in ("W1", "W2") for r in windows[window]["_records"]]
            all_block = _combined_window(windows, combined_records)
            windows["ALL"] = all_block
            # publication candidates: one entry per origin, the ALL block repeats W1+W2
            for window in ("W1", "W2"):
                published_records.extend(windows[window]["_records"])
            for window, block in windows.items():
                block.pop("_records", None)
            eligible = sum(windows[w]["origins_eligible"] for w in ("W1", "W2"))
            scored = sum(windows[w]["scored"] for w in ("W1", "W2"))
            available = sum(windows[w]["available"] for w in ("W1", "W2"))
            counted = scored if actuals is not None else available
            coverage_share = _round(counted / eligible) if eligible else None
            target_block[str(horizon)] = {
                "windows": windows,
                "totals": {"eligible": eligible, "scored": scored, "available": available,
                           "coverage_share": coverage_share},
            }
            result["points"]["by_target"][f"{target}/h{horizon}"] = {
                "eligible": eligible, "scored": scored, "available": available,
                "coverage_share": coverage_share,
            }
            result["points"]["scored"] += scored
            result["points"]["available"] += available
            result["points"]["eligible"] += eligible
        if target_block:
            result["targets"][target] = target_block
    result["values"] = publication_block(published_records, adapter.verdict, allow_values=False)
    return result


def _combined_window(windows: Mapping[str, Mapping], records: Sequence[Mapping]) -> dict:
    """W1+W2 aggregate block (same losses, counts added)."""
    eligible = sum(windows[w]["origins_eligible"] for w in ("W1", "W2"))
    available = sum(windows[w]["available"] for w in ("W1", "W2"))
    scored_records = [r for r in records if r.get("status") == "SCORED"]
    exclusions: dict[str, int] = {}
    for window in ("W1", "W2"):
        for reason, count in windows[window]["exclusions"].items():
            exclusions[reason] = exclusions.get(reason, 0) + count
    block = {
        "window": "ALL",
        "origins_eligible": eligible,
        "scored": len(scored_records),
        "available": available,
        "coverage_share": _round((len(scored_records) or available) / eligible) if eligible else None,
        "lookahead_excluded": sum(count for reason, count in exclusions.items() if reason in LOOKAHEAD_REASONS),
        "exclusions": dict(sorted(exclusions.items())),
    }
    block.update(score_records(scored_records))
    block["_records"] = list(records)
    return block


# ----------------------------------------------------------------------------------------------
# Report assembly
# ----------------------------------------------------------------------------------------------
def content_digest(report: Mapping) -> str:
    core = {key: value for key, value in report.items() if key not in ("generated_at", "content_sha256")}
    blob = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return sha256_text(blob)


def metric_definitions() -> dict:
    return {
        "rmse": "protocol losses.point: root mean squared error of (forecast - first publication)",
        "mae": "mean absolute error, reported as context next to RMSE",
        "bias": "mean signed error (forecast - actual); positive means the source over-predicts",
        "crps": ("protocol forecast_product.crps: twice the mean pinball loss over the 19 quantiles "
                 "(5%..95% in 5% steps); requires a predictive distribution, never imputed"),
        "brier": "protocol losses.event: mean squared error of a probability forecast of a binary event",
        "coverage_share": ("scored points divided by the origins eligible in the window: how much of "
                           "the window the source actually covers, not how accurate it is"),
        "coverage_90": "share of facts inside the source's 5-95% interval (only with a distribution)",
        "secondary": ("protocol actuals.secondary: the loss against the value in the vintage 12 months "
                      "after the period; reported separately, never pooled with the primary actual"),
    }


def _status_of(sources: Mapping[str, Mapping], total_points: int, scored_sources: Sequence[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if total_points == 0:
        return "NOT_AVAILABLE", ["NO_SCORED_POINTS"]
    if len(scored_sources) < MIN_SCORED_SOURCES:
        reasons.append(f"SCORED_SOURCES_BELOW_CRITERION({len(scored_sources)}<{MIN_SCORED_SOURCES})")
    if total_points < MIN_SCORED_POINTS:
        reasons.append(f"SCORED_POINTS_BELOW_CRITERION({total_points}<{MIN_SCORED_POINTS})")
    if reasons:
        return "PARTIAL", reasons
    return "OK", []


def build_report(*, data_root=None, horizons: Sequence[int] = (0, 1), max_origins: int | None = None,
                 generated_at: str | None = None, register: Mapping | None = None) -> dict:
    protocol_path = PROTOCOL_PATH
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    register_path = LICENCE_PATH
    register = register if register is not None else load_licence_register(register_path)
    adapters = build_adapters(data_root=data_root, register=register)
    actuals = ActualCache(data_root=data_root)
    report: dict = {
        "report_id": REPORT_ID,
        "version": VERSION,
        "tool": TOOL,
        "generated_at": generated_at or _utc_now(),
        "purpose": (
            "point-in-time scorecard of external forecasts against the first publication of the fact "
            "on the frozen protocol windows W1/W2: who is more accurate, when, and with what coverage"
        ),
        "publication_policy": {
            "third_party_values": REPORT_VALUES_POLICY,
            "note": (
                "even when the licence register would allow publishing a sample (NY Fed), the "
                "committed report keeps third-party forecast/actual values outside git; only "
                "aggregates, counts, metadata and hashes are written"
            ),
            "raw_data_location": "data/raw/ (git-ignored; fetched with tools/fetch_scorecard_data.py)",
        },
        "status": None,
        "status_reasons": [],
        "protocol": {
            "file": _relpath(protocol_path),
            "protocol_id": protocol.get("protocol_id"),
            "version": protocol.get("version"),
            "status": protocol.get("status"),
            "sha256": sha256_file(protocol_path),
            "windows": {
                window: dict(protocol["windows"][window]) for window in ("W1", "W2")
            },
            "actuals_rule": protocol["actuals"]["rule"],
            "losses": protocol["losses"],
        },
        "licence_register": {
            "file": _relpath(register_path),
            "sha256": sha256_file(register_path),
            "entries_used": {source_id: spec["licence_entry"] for source_id, spec in SOURCE_SPECS.items()},
        },
        "pit_rules": {
            "publication_date": (
                "a forecast is usable only if its publication date is on or before the origin "
                "(origin_policy.default); for the SPF the date is the official news release"
            ),
            "no_lookahead": "publication_date > origin -> LOOKAHEAD_REJECTED (or the source-specific reason)",
            "actuals": (
                "primary actual = first publication from the ALFRED vintage archive; a point whose "
                "first-publication vintage is at or before the origin is rejected"
            ),
            "windows": "an origin belongs to a window only if the origin and the target period are inside it",
            "covid_exclusion": protocol["windows"]["origin_exclusion"]["covid_targets"],
            "quantiles": "a distribution is scored only on the protocol 5-95% grid with non-crossing quantiles",
        },
        "metric_definitions": metric_definitions(),
        "targets": [target.get("id") for target in protocol.get("targets") or []],
        "horizons": sorted(int(h) for h in horizons),
        "sources": {},
        "criteria": {
            "min_scored_sources": MIN_SCORED_SOURCES,
            "min_scored_points": MIN_SCORED_POINTS,
            "m2_min_scored_sources": M2_MIN_SCORED_SOURCES,
            "source": "docs/173 P0-1 MVP asks for >=3 scorable sources; docs/207 M2 asks for >=2",
        },
        "gaps": [],
        "limitations": [
            "MVP scope: T2_GDP_GROWTH only. SPF contributes h=0 and h=1; NY Fed Staff Nowcast h=0; "
            "Livingston Survey h=0 only (strict post-2004 subset). T1/T3 are not scored yet.",
            "the SPF file is the latest revision of the medians, not a point-in-time vintage snapshot: "
            "the release date fixes availability, errata after the survey are not audited",
            "NY Fed Staff Nowcast is a values-only archive: 2002Q1-2015Q4 are historical "
            "reconstructions (excluded from real-time scoring) and only 2016Q1-2021Q3 rows are as "
            "published; the archive has no underlying series, so no re-estimation is possible",
            "Livingston Survey is semiannual and only the strict post-2004 subset is scored: 32 PIT "
            "points (W1 6, W2 26). Its forecast is 400*ln(RGDPX_ZM/RGDPX_BP) derived from published "
            "median levels, a documented transformation rather than a separately published median "
            "growth forecast; T1 and T2 h>0 are not mapped, and the pre-2004 base values are excluded "
            "because the documentation warns that X_BP is unreliable for variables other than CPI",
            "SPF forecasts are point (median) forecasts: CRPS and interval coverage are NOT_AVAILABLE "
            "for that source, and no distribution is imputed to make a number appear",
            "no external source in this MVP publishes a probability for a protocol event, so Brier is "
            "implemented and tested but NOT_AVAILABLE for every scored source",
            "GDPNow is RESTRICTED and is not used; its model quantity is represented by the project's "
            "own OPEN OWN_NOWCAST_BRIDGE, which is a different object and is not a like-for-like substitute",
            "the scorecard contains external forecasts only: joining the project's own baselines is a "
            "separate step (docs/173 'external vs ours'), not part of this report",
            "origins inside one quarter overlap (h=0 uses every month end), so the effective number of "
            "independent observations is smaller than the scored point count; no significance test is "
            "reported here",
        ],
        "next_steps": [
            "add the project's own OPEN baselines (C6 forward-registry rows, R3 ensemble) to the same "
            "origins to produce the 'external vs ours' table",
            "extend to h=2/h=4 and T1/T3 once a source with a matching horizon/event mapping is "
            "registered; Livingston currently supports only T2 h=0 on the strict post-2004 subset",
            "widen Livingston coverage only through a protocol decision: T1 mapping would need a series/"
            "seasonality decision, and pre-2004 points stay excluded because of the documented X_BP caveat",
            "turn the report into the UC-44 subscription artifact: per-source error tables, charts and a "
            "stable API payload now that three sources are scored",
        ],
    }
    for source_id, adapter in adapters.items():
        report["sources"][source_id] = _score_source(adapter, protocol, horizons=horizons,
                                                     actuals=actuals, max_origins=max_origins)
    # cross-source summary and honest status
    scored_sources = [sid for sid, block in report["sources"].items()
                      if block["points"]["scored"] > 0]
    total_points = sum(block["points"]["scored"] for block in report["sources"].values())
    status, reasons = _status_of(report["sources"], total_points, scored_sources)
    report["status"] = status
    report["status_reasons"] = reasons
    report["cross_source"] = {
        "scored_sources": scored_sources,
        "scored_sources_count": len(scored_sources),
        "scored_points_total": total_points,
        "eligible_origins_total": sum(block["points"]["eligible"] for block in report["sources"].values()),
        "sources_by_status": {sid: block["status"]["status"] for sid, block in report["sources"].items()},
        "criteria_pass": {
            "sources": len(scored_sources) >= MIN_SCORED_SOURCES,
            "points": total_points >= MIN_SCORED_POINTS,
            "m2_sources": len(scored_sources) >= M2_MIN_SCORED_SOURCES,
        },
        "licence_compliance": {
            "restricted_used": [sid for sid, block in report["sources"].items()
                                if block["metadata"]["licence"]["usage"] == "NOT_USED"
                                and block["points"]["scored"] > 0],
            "values_published": [sid for sid, block in report["sources"].items()
                                 if block["values"].get("values_published")],
        },
    }
    report["gaps"] = _gaps(report)
    report["content_sha256"] = content_digest(report)
    return report


def _gaps(report: Mapping) -> list[str]:
    gaps: list[str] = []
    for source_id, block in report["sources"].items():
        status = block["status"]
        if status["status"] != "AVAILABLE":
            gaps.append(f"{source_id}: {status['status']} ({status['reason']})")
        elif block["points"]["scored"] == 0:
            gaps.append(f"{source_id}: AVAILABLE but no point passed the PIT/actual gates")
    if report["cross_source"]["scored_sources_count"] < MIN_SCORED_SOURCES:
        count = report["cross_source"]["scored_sources_count"]
        m2 = "met" if count >= M2_MIN_SCORED_SOURCES else "NOT met"
        gaps.append(
            f"only {count} of {len(report['sources'])} catalogue sources could be scored; "
            f"docs/207 M2 (>={M2_MIN_SCORED_SOURCES}) is {m2}, docs/173 MVP asks for >={MIN_SCORED_SOURCES}"
        )
    gaps.append("project's own baselines are not joined to these origins yet ('external vs ours' is open)")
    gaps.append("CRPS and Brier are NOT_AVAILABLE for every scored source (point-only, no event probability)")
    return gaps


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def _strip_for_check(block: Mapping) -> dict:
    return {
        "status": block["status"],
        "counts_mode": block.get("counts_mode"),
        "points": block["points"],
        "targets": {
            target: {horizon: {window: {"eligible": w["origins_eligible"], "available": w["available"],
                                        "coverage_share": w["coverage_share"],
                                        "exclusions": w["exclusions"]}
                               for window, w in hblock["windows"].items()}
                     for horizon, hblock in horizons.items()}
            for target, horizons in block["targets"].items()
        },
        "values_published": block["values"].get("values_published"),
        "licence": {key: block["metadata"]["licence"].get(key)
                    for key in ("status", "usage", "values_publishable", "publication_blocker")},
    }


def check_plan(*, data_root=None, horizons: Sequence[int] = (0, 1)) -> dict:
    """Fast metadata/availability summary: the publication gate runs, no fact is loaded.

    The counts are ``available`` (forecast public at the origin), not ``scored``: the facts are
    fetched only by the full run, so ``--check`` stays cheap enough for CI smoke use.
    """
    register = load_licence_register()
    adapters = build_adapters(data_root=data_root, register=register)
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    sources = {}
    for source_id, adapter in adapters.items():
        block = _score_source(adapter, protocol, horizons=horizons, actuals=None)
        sources[source_id] = _strip_for_check(block)
    available_sources = [sid for sid, block in sources.items() if block["points"]["available"] > 0]
    total_points = sum(block["points"]["available"] for block in sources.values())
    status, reasons = _status_of(sources, total_points, available_sources)
    return {
        "report_id": REPORT_ID,
        "status": status,
        "status_reasons": reasons,
        "protocol": {"file": _relpath(PROTOCOL_PATH), "version": protocol.get("version"),
                     "sha256": sha256_file(PROTOCOL_PATH)},
        "sources": sources,
        "available_points_total": total_points,
        "counts_mode": "available",
        "note": ("metadata/availability summary only: the publication gate is applied but the facts "
                 "are not loaded; the full run writes the scored report"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="External forecast PIT scorecard (task-54, docs/174)")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--horizons", nargs="+", type=int, default=[0, 1])
    parser.add_argument("--max-origins", type=int, default=None,
                        help="score at most N origins per window (tests/smoke runs only)")
    parser.add_argument("--check", action="store_true",
                        help="print the metadata/availability summary and exit (no scoring)")
    args = parser.parse_args(argv)
    horizons = sorted({int(h) for h in args.horizons})
    unsupported = [h for h in horizons if h not in SPF_GDP_CODE_FOR_HORIZON]
    if unsupported:
        raise SystemExit(f"unsupported horizon(s) {unsupported}; the T2 grid is 0,1,2,4")
    if args.check:
        summary = check_plan(data_root=Path(args.data_root), horizons=horizons)
        print(json.dumps(summary, ensure_ascii=False, indent=1, default=_json_default))
        return 0 if summary["status"] == "OK" else 3
    report = build_report(data_root=Path(args.data_root), horizons=horizons,
                          max_origins=args.max_origins)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=_json_default) + "\n",
                   encoding="utf-8", newline="\n")
    summary = {
        "out": _relpath(out),
        "status": report["status"],
        "status_reasons": report["status_reasons"],
        "scored_sources": report["cross_source"]["scored_sources"],
        "scored_points_total": report["cross_source"]["scored_points_total"],
        "sources_by_status": report["cross_source"]["sources_by_status"],
        "rmse": {
            source_id: {
                f"{target}/h{horizon}/{window}": block["metrics"]["rmse"]
                for target, horizons in source["targets"].items()
                for horizon, hblock in horizons.items()
                for window, block in hblock["windows"].items()
            }
            for source_id, source in report["sources"].items() if source["targets"]
        },
        "content_sha256": report["content_sha256"][:16],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
