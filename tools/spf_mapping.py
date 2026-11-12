#!/usr/bin/env python3
"""Official SPF horizon mapping and release-availability rules (metadata-only tool).

Purpose
-------
The ensemble layer needs two facts about the Philadelphia Fed Survey of Professional
Forecasters (SPF): *what* a published horizon code means and *when* that median is
available.  This module records both from the official SPF documentation and release
calendar, independent of the declared mapping in ``src/amfe/ensemble/sources.py``.

Third-party median values are NEVER written by this tool.  They are read only from
the git-ignored ``data/raw/spf/spf_median.json``; the committed output is mapping
metadata/verdicts only.

Official sources (accessed 2026-11-12)
--------------------------------------
* SPF Documentation (PDF, Last Update July 28, 2026), p. 6 (deadlines), p. 7
  (release dates), p. 28 (growth-rate codes), pp. 35-36 (inflation horizons):
  https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/spf-documentation.pdf
* Official deadline/news-release table:
  https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/survey-of-professional-forecasters/spf-release-dates.txt
* Median forecasts landing page:
  https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VALUES = ROOT / "data" / "raw" / "spf" / "spf_median.json"
DEFAULT_METADATA = ROOT / "protocols" / "spf_metadata.json"

SPF_DOCUMENTATION_URL = (
    "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/"
    "survey-of-professional-forecasters/spf-documentation.pdf"
)
SPF_RELEASE_DATES_URL = (
    "https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/"
    "survey-of-professional-forecasters/spf-release-dates.txt"
)
SPF_PDF_LAST_UPDATE = "July 28, 2026"
SPF_ACCESSED = "2026-11-12"

#: Official SPF growth codes: appended number 2 = quarter of the survey; 3..6 = next four quarters.
SPF_GDP_CODES = ("drgdp2", "drgdp3", "drgdp4", "drgdp5", "drgdp6")
SPF_GDP_OFFSET = {"drgdp2": 0, "drgdp3": 1, "drgdp4": 2, "drgdp5": 3, "drgdp6": 4}
#: ``RGDP1`` is a historical level, not a forecast column; there is no ``drgdp1``
#: (PDF p.40: "RGDP1 refers ... to the historical value for the quarter before the
#: quarter in which the survey is conducted").
NOT_A_FORECAST_CODE = {
    "drgdp1": "no such SPF forecast column; RGDP1 is the historical level for the quarter before the survey",
}
#: Protocol v4 T2 horizons (quarters) -> official SPF code.  h=3 is not on the protocol grid.
PROTOCOL_T2_HORIZONS = (0, 1, 2, 4)
PROTOCOL_T2_CODE = {0: "drgdp2", 1: "drgdp3", 2: "drgdp4", 4: "drgdp6"}
#: Inverse for reporting; drgdp5 maps to protocol horizon 3, which has no T2 entry.
SPF_CODE_TO_PROTOCOL_HORIZON = {"drgdp2": 0, "drgdp3": 1, "drgdp4": 2, "drgdp5": 3, "drgdp6": 4}
SPF_INFLATION_CODES = ("INFCPI1YR", "INFCPI10YR")
SPF_ALL_CODES = SPF_GDP_CODES + SPF_INFLATION_CODES
SPF_MIDDLE_MONTHS = {2: 1, 5: 2, 8: 3, 11: 4}  # month -> survey quarter number (1..4)
SPF_REAL_TIME_START = "1990Q3"  # 1990:Q2 was not conducted in real time (PDF p.6)

#: Exact official "True Deadline Date" anchors from spf-release-dates.txt.
#: Only a subset is embedded; the fallback is documented and deliberately conservative.
DEADLINE_ANCHORS = {
    "1990Q3": date(1990, 8, 23),
    "1990Q4": date(1990, 11, 22),
    "1991Q1": date(1991, 2, 16),
    "2000Q1": date(2000, 2, 12),
    "2005Q1": date(2005, 2, 9),
    "2005Q2": date(2005, 5, 12),
    "2013Q4": date(2013, 11, 18),
    "2019Q1": date(2019, 3, 12),
    "2020Q1": date(2020, 2, 11),
    "2026Q1": date(2026, 3, 2),
    "2026Q2": date(2026, 5, 12),
    "2026Q3": date(2026, 8, 11),
}

#: Exact official "News Release Date" anchors from spf-release-dates.txt.
RELEASE_ANCHORS = {
    "1990Q3": date(1990, 8, 31),
    "1990Q4": date(1990, 11, 28),
    "1991Q1": date(1991, 2, 21),
    "2000Q1": date(2000, 2, 22),
    "2005Q1": date(2005, 2, 14),
    "2005Q2": date(2005, 5, 16),
    "2013Q4": date(2013, 11, 25),
    "2019Q1": date(2019, 3, 22),
    "2020Q1": date(2020, 2, 14),
    "2026Q1": date(2026, 3, 6),
    "2026Q2": date(2026, 5, 15),
    "2026Q3": date(2026, 8, 14),
}

#: Machine-readable official definitions.  Quotes and page numbers are in docs/86.
VARIABLE_DEFINITIONS = {
    "drgdp2": {
        "variable": "Real GDP growth",
        "official_definition": "quarter-over-quarter annualized growth of median real GDP level; number 2 = quarter in which the survey is conducted",
        "offset_quarters": 0,
        "pdf_page": 28,
        "usage": "T2_GDP_GROWTH h=0",
    },
    "drgdp3": {
        "variable": "Real GDP growth",
        "official_definition": "quarter-over-quarter annualized growth of median real GDP level; number 3 = first quarter after the survey quarter",
        "offset_quarters": 1,
        "pdf_page": 28,
        "usage": "T2_GDP_GROWTH h=1",
    },
    "drgdp4": {
        "variable": "Real GDP growth",
        "official_definition": "quarter-over-quarter annualized growth of median real GDP level; number 4 = second quarter after the survey quarter",
        "offset_quarters": 2,
        "pdf_page": 28,
        "usage": "T2_GDP_GROWTH h=2",
    },
    "drgdp5": {
        "variable": "Real GDP growth",
        "official_definition": "quarter-over-quarter annualized growth of median real GDP level; number 5 = third quarter after the survey quarter",
        "offset_quarters": 3,
        "pdf_page": 28,
        "usage": "not on the protocol T2 horizon grid (h=3 absent)",
    },
    "drgdp6": {
        "variable": "Real GDP growth",
        "official_definition": "quarter-over-quarter annualized growth of median real GDP level; number 6 = fourth quarter after the survey quarter",
        "offset_quarters": 4,
        "pdf_page": 28,
        "usage": "T2_GDP_GROWTH h=4",
    },
    "INFCPI1YR": {
        "variable": "CPI inflation",
        "official_definition": "annual-average CPI inflation over the four quarters following the survey quarter (geometric average of the Q/Q median CPI forecasts CPI3..CPI6), annualized percentage points",
        "offset_quarters": None,
        "pdf_page": "35-36",
        "usage": "not a monthly y/y CPI forecast; no T1 mapping",
    },
    "INFCPI10YR": {
        "variable": "CPI inflation",
        "official_definition": "annual-average rate of CPI inflation over the next 10 years; the Section 5 text does not spell out the exact start/end year",
        "offset_quarters": None,
        "pdf_page": 35,
        "usage": "not a monthly y/y CPI forecast and not a protocol horizon; no T1 mapping",
    },
}

UNSUPPORTED_TARGETS = {
    "T1_CPI_YOY": "NO_MONTHLY_YOY_CPI_FORECAST",
    "T3_RECESSION": "NO_NBER_RECESSION_PROBABILITY_AND_EVENT_MISMATCH",
}


class SpfMappingError(ValueError):
    """Invalid SPF mapping/availability request."""


class SpfValuesUnavailable(RuntimeError):
    """The git-ignored SPF value file is absent or unusable."""


@dataclass(frozen=True)
class Availability:
    survey_quarter: str | None
    origin: date
    available: bool
    reason: str
    deadline: date | None
    release: date | None
    deadline_reached: bool
    release_reached: bool

    def to_dict(self) -> dict:
        return {
            "survey_quarter": self.survey_quarter,
            "origin": self.origin.isoformat(),
            "available": self.available,
            "reason": self.reason,
            "deadline": None if self.deadline is None else self.deadline.isoformat(),
            "release": None if self.release is None else self.release.isoformat(),
            "deadline_reached": self.deadline_reached,
            "release_reached": self.release_reached,
        }


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise SpfMappingError(f"invalid date {value!r}") from exc


def _parse_quarter(label: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{4})Q([1-4])", str(label).strip())
    if not match:
        raise SpfMappingError(f"invalid survey quarter {label!r}; expected YYYYQn")
    return int(match.group(1)), int(match.group(2))


def quarter_label(quarter: int) -> str:
    if quarter < 0:
        raise SpfMappingError("quarter index must be non-negative")
    return f"{quarter // 4}Q{quarter % 4 + 1}"


def quarter_index(label: str) -> int:
    year, q = _parse_quarter(label)
    return year * 4 + (q - 1)


def survey_quarter_for_origin(origin) -> str | None:
    """The survey quarter whose deadline month contains the origin (middle month only)."""
    d = _as_date(origin)
    if d.month not in SPF_MIDDLE_MONTHS:
        return None
    return f"{d.year}Q{SPF_MIDDLE_MONTHS[d.month]}"


def _last_day_of_middle_month(label: str) -> date:
    year, quarter = _parse_quarter(label)
    month = {1: 2, 2: 5, 3: 8, 4: 11}[quarter]
    return date(year, month, calendar.monthrange(year, month)[1])


def _is_real_time_quarter(label: str) -> bool:
    return quarter_index(label) >= quarter_index(SPF_REAL_TIME_START)


def deadline_date(survey_quarter: str) -> date:
    """Official deadline date; embedded anchors, else a conservative month-end fallback."""
    label = str(survey_quarter).strip()
    _parse_quarter(label)
    if label in DEADLINE_ANCHORS:
        return DEADLINE_ANCHORS[label]
    return _last_day_of_middle_month(label)


def release_date(survey_quarter: str) -> date:
    """Official news-release date; embedded anchors, else a conservative month-end fallback."""
    label = str(survey_quarter).strip()
    _parse_quarter(label)
    if label in RELEASE_ANCHORS:
        return RELEASE_ANCHORS[label]
    return _last_day_of_middle_month(label)


def availability(origin, survey_quarter: str | None = None) -> Availability:
    """When the median of ``survey_quarter`` is usable at ``origin``.

    The SPF values become public at the news release, not at the response deadline;
    availability therefore requires ``origin >= release_date``.  The deadline is
    reported separately.  Only the middle month of a survey quarter is accepted as
    an alignment origin (same convention as ``ensemble.sources``), and the not-
    real-time 1990:Q2 (and earlier) values are explicitly unavailable.
    """
    origin_d = _as_date(origin)
    derived = survey_quarter_for_origin(origin_d)
    label = survey_quarter if survey_quarter is not None else derived
    if label is None:
        return Availability(None, origin_d, False, "ORIGIN_NOT_AN_SPF_MIDDLE_MONTH", None, None, False, False)
    try:
        _parse_quarter(label)
    except SpfMappingError as exc:
        raise SpfMappingError(str(exc)) from exc
    if derived is None:
        return Availability(label, origin_d, False, "ORIGIN_NOT_AN_SPF_MIDDLE_MONTH", None, None, False, False)
    if derived != label:
        return Availability(label, origin_d, False, "ORIGIN_QUARTER_MISMATCH", None, None, False, False)
    if not _is_real_time_quarter(label):
        return Availability(label, origin_d, False, "SPF_NOT_REAL_TIME_BEFORE_1990Q3", None, None, False, False)
    deadline = deadline_date(label)
    release = release_date(label)
    release_reached = origin_d >= release
    return Availability(
        survey_quarter=label,
        origin=origin_d,
        available=release_reached,
        reason="AVAILABLE" if release_reached else "SPF_RELEASE_NOT_REACHED_AT_ORIGIN",
        deadline=deadline,
        release=release,
        deadline_reached=origin_d >= deadline,
        release_reached=release_reached,
    )


def parse_values(payload: Mapping) -> dict[str, dict[str, float]]:
    """Validate a local SPF values payload (``series`` list) without writing anything."""
    if not isinstance(payload, Mapping):
        raise SpfValuesUnavailable("SPF values payload must be a mapping")
    rows = payload.get("series")
    if not isinstance(rows, list) or not rows:
        raise SpfValuesUnavailable("SPF values file has no non-empty 'series' list")
    table: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = row.get("code")
        quarter = row.get("survey_quarter")
        raw_value = row.get("value")
        if code not in SPF_ALL_CODES or not isinstance(quarter, str):
            continue
        try:
            _parse_quarter(quarter)
            value = float(raw_value)
        except (TypeError, ValueError, SpfMappingError):
            continue
        if not math.isfinite(value):
            continue
        key = (str(code), quarter)
        if key in seen:
            raise SpfValuesUnavailable(f"duplicate SPF observation {key}")
        seen.add(key)
        table.setdefault(str(code), {})[quarter] = value
    if not table:
        raise SpfValuesUnavailable("SPF values file has no usable observations")
    return table


def load_values(path: Path | str | None = None) -> dict[str, dict[str, float]]:
    """Load the git-ignored values file; absent file is an explicit skip condition."""
    values_path = Path(path) if path is not None else DEFAULT_VALUES
    if not values_path.exists():
        raise SpfValuesUnavailable(
            f"SPF values are absent locally: {values_path} "
            "(third-party values are git-ignored; run tools/fetch_spf.py or skip)"
        )
    try:
        payload = json.loads(values_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpfValuesUnavailable(f"cannot read SPF values {values_path}: {exc}") from exc
    return parse_values(payload)


def mapping_table() -> dict:
    """Protocol-facing mapping, including explicit unsupported targets and h=3."""
    return {
        "official_sources": {
            "documentation": SPF_DOCUMENTATION_URL,
            "documentation_last_update": SPF_PDF_LAST_UPDATE,
            "release_dates": SPF_RELEASE_DATES_URL,
            "accessed": SPF_ACCESSED,
        },
        "growth_codes": {
            code: {
                "offset_quarters": SPF_GDP_OFFSET[code],
                "definition": VARIABLE_DEFINITIONS[code]["official_definition"],
            }
            for code in SPF_GDP_CODES
        },
        "not_a_forecast_code": dict(NOT_A_FORECAST_CODE),
        "T2_GDP_GROWTH": {str(h): PROTOCOL_T2_CODE[h] for h in PROTOCOL_T2_HORIZONS},
        "T2_not_on_grid": {"3": "drgdp5 (h=3 has no protocol T2 horizon entry)"},
        "T1_CPI_YOY": {
            "available": False,
            "reason": UNSUPPORTED_TARGETS["T1_CPI_YOY"],
            "note": "INFCPI1YR is a four-quarter annual average and INFCPI10YR a 10-year average; neither is monthly y/y CPI",
        },
        "T3_RECESSION": {
            "available": False,
            "reason": UNSUPPORTED_TARGETS["T3_RECESSION"],
            "note": "SPF RECESS/Anxious Index is a GDP-decline probability, not an NBER recession event",
        },
        "inflation_codes": {
            code: {
                "definition": VARIABLE_DEFINITIONS[code]["official_definition"],
                "mapped_to_protocol_target": None,
            }
            for code in SPF_INFLATION_CODES
        },
    }


def metadata_check(path: Path | str | None = None) -> dict:
    """Compare committed SPF metadata with the official mapping (no values needed)."""
    metadata_path = Path(path) if path is not None else DEFAULT_METADATA
    issues: list[str] = []
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"metadata": str(metadata_path), "ok": False, "issues": [f"cannot read metadata: {exc}"]}
    variables = {v.get("code") for v in metadata.get("variables", []) if isinstance(v, dict)}
    missing = [code for code in SPF_ALL_CODES if code not in variables]
    if missing:
        issues.append(f"metadata lacks variables: {missing}")
    for file_entry in metadata.get("files", []):
        if not isinstance(file_entry, dict):
            continue
        for column in file_entry.get("columns_used", []):
            if column not in SPF_ALL_CODES:
                issues.append(f"metadata file {file_entry.get('name')} uses unknown column {column!r}")
    if metadata.get("raw_files_committed") is not False:
        issues.append("metadata must state raw_files_committed=false")
    if "series" in metadata:
        issues.append("metadata must not contain third-party value rows")
    return {"metadata": str(metadata_path), "ok": not issues, "issues": issues,
            "mapped_codes": list(SPF_ALL_CODES), "metadata_variables": sorted(v for v in variables if v)}


def forecast_at(
    target: str,
    origin,
    horizon: int,
    *,
    values_path: Path | str | None = None,
) -> dict:
    """One SPF median for a protocol target/horizon at an origin, with explicit verdict.

    Returns a plain dict (never raises for absent local values) so callers can show
    ``SPF_VALUES_NOT_DOWNLOADED`` rather than silently dropping the member.
    """
    target = str(target)
    horizon_i = int(horizon)
    origin_d = _as_date(origin)
    base = {
        "target": target,
        "origin": origin_d.isoformat(),
        "horizon": horizon_i,
        "source": "PHILADELPHIA_FED_SPF_MEDIAN",
        "values_location": "data/raw/spf/spf_median.json (git-ignored)",
        "attribution": "Source: Federal Reserve Bank of Philadelphia, Survey of Professional Forecasters",
    }
    if target in UNSUPPORTED_TARGETS:
        return {**base, "available": False, "reason": UNSUPPORTED_TARGETS[target], "code": None, "value": None}
    if target != "T2_GDP_GROWTH":
        return {**base, "available": False, "reason": f"UNSUPPORTED_TARGET_{target}", "code": None, "value": None}
    code = PROTOCOL_T2_CODE.get(horizon_i)
    if code is None:
        return {
            **base,
            "available": False,
            "reason": f"NO_SPF_HORIZON_CODE_FOR_H{horizon_i}",
            "code": None,
            "value": None,
        }
    verdict = availability(origin_d)
    if not verdict.available:
        return {
            **base,
            "available": False,
            "reason": verdict.reason,
            "code": code,
            "value": None,
            "survey_quarter": verdict.survey_quarter,
            "deadline": None if verdict.deadline is None else verdict.deadline.isoformat(),
            "release": None if verdict.release is None else verdict.release.isoformat(),
            "mapping": f"T2 h={horizon_i} -> {code}; drgdp2=current survey quarter",
        }
    try:
        table = load_values(values_path)
    except SpfValuesUnavailable as exc:
        return {
            **base,
            "available": False,
            "reason": "SPF_VALUES_NOT_DOWNLOADED",
            "code": code,
            "value": None,
            "survey_quarter": verdict.survey_quarter,
            "note": str(exc),
            "mapping": f"T2 h={horizon_i} -> {code}; drgdp2=current survey quarter",
        }
    quarter = verdict.survey_quarter
    value = table.get(code, {}).get(quarter)
    if value is None:
        return {
            **base,
            "available": False,
            "reason": "SURVEY_QUARTER_NOT_IN_SPF_FILE",
            "code": code,
            "value": None,
            "survey_quarter": quarter,
            "mapping": f"T2 h={horizon_i} -> {code}; drgdp2=current survey quarter",
        }
    return {
        **base,
        "available": True,
        "reason": "AVAILABLE",
        "code": code,
        "value": value,
        "survey_quarter": quarter,
        "deadline": verdict.deadline.isoformat() if verdict.deadline else None,
        "release": verdict.release.isoformat() if verdict.release else None,
        "mapping": f"T2 h={horizon_i} -> {code}; drgdp2=current survey quarter",
    }


def check(values_path: Path | str | None = None, metadata_path: Path | str | None = None) -> dict:
    """Offline check: metadata, mapping, official anchors and optional local values."""
    meta = metadata_check(metadata_path)
    result = {
        "ok": bool(meta["ok"]),
        "mapping": mapping_table(),
        "metadata": meta,
        "deadline_anchors": {q: d.isoformat() for q, d in sorted(DEADLINE_ANCHORS.items())},
        "release_anchors": {q: d.isoformat() for q, d in sorted(RELEASE_ANCHORS.items())},
        "values": {"available": False, "reason": None},
    }
    try:
        table = load_values(values_path)
    except SpfValuesUnavailable as exc:
        result["values"] = {"available": False, "reason": str(exc)}
        return result
    result["values"] = {
        "available": True,
        "codes": {code: {"quarters": len(rows), "first": min(rows), "last": max(rows)}
                  for code, rows in sorted(table.items())},
        "total_observations": sum(len(rows) for rows in table.values()),
    }
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Official SPF horizon mapping and availability (values stay git-ignored)")
    parser.add_argument("--check", action="store_true", help="print metadata/mapping/local-values summary")
    parser.add_argument("--values", default=str(DEFAULT_VALUES), help="git-ignored SPF median JSON")
    parser.add_argument("--metadata", default=str(DEFAULT_METADATA), help="committed metadata JSON")
    parser.add_argument("--origin", help="origin date YYYY-MM-DD")
    parser.add_argument("--target", default="T2_GDP_GROWTH")
    parser.add_argument("--horizon", type=int, default=0)
    args = parser.parse_args(argv)
    if args.check or not args.origin:
        payload = check(args.values, args.metadata)
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        if not payload["metadata"]["ok"]:
            return 1
        return 0
    print(json.dumps(forecast_at(args.target, args.origin, args.horizon, values_path=args.values),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
