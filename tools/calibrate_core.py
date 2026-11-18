#!/usr/bin/env python3
"""Calibrate the world-core aggregates from the retrieved US macro series (stage 4-6).

The tool reads the git-ignored retrieval output (``data/raw/<SERIES>/latest.csv``, written by
``tools/fetch_data.py``) and estimates a small, explicitly specified set of *aggregates* that the
world core (docs/79, docs/80) can use as starting values.  It publishes only derived statistics
(means, shares, growth trends, residuals), never the raw observations - the licence policy in
``protocols/evaluation_protocol_v4.json`` allows derived statistics in the repository and keeps the
raw third-party series in the ignored data directory / the private CI artifact.

Parameters (5, each with its own window, sources and uncertainty)
-----------------------------------------------------------------
``gov_share``            GCEC1 / GDPC1 - real government consumption and gross investment as a share
                        of real GDP (BEA NIPA).  The government-size starting value for the fiscal layer.
``investment_share``     GPDIC1 / GDPC1 - real gross private domestic investment share (BEA).
``import_share``         IMPGSC1 / GDPC1 - real imports of goods and services share (BEA); the
                        openness starting value (exports are reported alongside, not as a parameter).
``nairu_proxy``          mean UNRATE over 2015-2019 (a low-inflation, sustained-expansion window) as a
                        NAIRU *proxy*, explicitly not an estimate of the natural rate.
``productivity_trend``   annualised OLS trend of log(GDPC1 / PAYEMS) over the baseline window: real
                        output per employee, not a labour theory of value (docs/35, docs/37).

V2 checks on real series (identity-type evidence for the physical/fiscal layer)
-------------------------------------------------------------------------------
``V2_additivity_levels``     Y = C + I + G + NX in chained 2017$: chain-linked components are not
                             additive, so the residual is expected to be small but non-zero - the check
                             measures it instead of pretending the identity holds exactly.
``V2_additivity_growth``     the same decomposition in first differences scaled by lagged GDP
                             ("в темповых приближениях"): residual statistics over the sample.
``V2_gdp_growth_series``     GDPC1-derived q/q annualised growth vs the separately published
                             A191RL1Q225SBEA series (rounding-level agreement).
``V2_nairu_sanity``          the NAIRU proxy lies inside the observed UNRATE range of the baseline and
                             is not below the sample minimum.
``V2_productivity_xcheck``   per-worker output trend from GDPC1/PAYEMS vs INDPRO/PAYEMS: the two
                             activity measures should agree in sign and broadly in size.
``V2_inflation_measures``    CPI vs core PCE vs GDP deflator growth: high but imperfect association by
                             construction (CONDITIONAL, not an identity).
``NOT_APPLICABLE`` rows record what these aggregates cannot identify: behavioural elasticities /
factor-income shares (need structural estimation and income-side data) and world/cross-country
aggregation (the plan retrieves US series only).  They are NOT_APPLICABLE, never PASS.

Reproducibility
---------------
The report carries the SHA-256 of every source file and a ``content_sha256`` over the parameters,
aggregates and V2 results (``generated_at`` excluded), so the calibration is checkable and the
tests can assert determinism.  The raw files are not committed: reproduce by downloading the
private ``amfe-data-<run>`` artifact of the data-refresh workflow into ``data/raw`` and re-running
``python tools/calibrate_core.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "protocols" / "data_plan.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"
DEFAULT_OUT = ROOT / "protocols" / "core_calibration.json"

CALIBRATION_ID = "AMFE-CORE-CALIBRATION"
VERSION = "0.1.0"

#: Series used by the calibration.  Every one of them is in protocols/data_plan.json and is
#: retrieved by tools/fetch_data.py; the source register entry, level and licence label are
#: recorded in the plan (see docs/92 for the selection rationale).
REQUIRED = ("GDPC1", "GCEC1", "GPDIC1", "EXPGSC1", "IMPGSC1", "PCECC96", "PAYEMS", "UNRATE",
            "A191RL1Q225SBEA", "INDPRO", "CPIAUCSL", "PCEPILFE", "GDPDEF")
OPTIONAL = ("RSAFS", "GCE")
DEFAULT_BASELINE_START = "1990Q1"
DEFAULT_BASELINE_END = "2019Q4"
DEFAULT_RECENT_START = "2020Q1"

#: V2 acceptance thresholds (documented in docs/92; the values are project choices, not data).
V2_THRESHOLDS = {
    "additivity_levels_max": 0.010,       # max |Y-(C+I+G+NX)| / Y over the sample
    "additivity_levels_mean": 0.003,      # mean |...| / Y
    "additivity_growth_median": 0.002,    # median |Δ-residual| / Y_lag
    "additivity_growth_max": 0.020,
    "gdp_growth_mean_abs": 0.05,          # percentage points; 0.1 rounding gives ~0.025 mean
    "productivity_corr": 0.50,
    "inflation_corr": 0.50,
}


class CalibrationError(RuntimeError):
    """Required series are missing or a series cannot be parsed."""


# ----------------------------------------------------------------------------------------------
# Series parsing and calendar helpers (standard library only)
# ----------------------------------------------------------------------------------------------
def _parse_date(text: str) -> Tuple[int, int, int]:
    parts = text.strip().split("-")
    if len(parts) != 3:
        raise CalibrationError(f"bad observation date {text!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def month_index(text: str) -> int:
    year, month, _ = _parse_date(text)
    return year * 12 + (month - 1)


def month_label(index: int) -> str:
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def quarter_index(text: str) -> int:
    year, month, _ = _parse_date(text)
    return year * 4 + (month - 1) // 3


def quarter_label(index: int) -> str:
    year, quarter = divmod(index, 4)
    return f"{year:04d}Q{quarter + 1}"


def quarter_start(index: int) -> str:
    year, quarter = divmod(index, 4)
    return f"{year:04d}-{quarter * 3 + 1:02d}-01"


def read_fred_csv(path: Path) -> Dict[str, float]:
    """Read a FRED ``observation_date,SERIES`` CSV; missing cells are dropped, not zeroed."""
    if not path.exists():
        raise CalibrationError(f"missing data file: {path}")
    values: Dict[str, float] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:  # pragma: no cover - filesystem errors
        raise CalibrationError(f"cannot read {path}: {exc}") from exc
    if not lines or "," not in lines[0]:
        raise CalibrationError(f"{path} is not a FRED CSV file")
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date = parts[0].strip()[:10]
        cell = parts[1].strip()
        if cell in ("", "."):
            continue
        try:
            values[date] = float(cell)
        except ValueError:
            continue
    if not values:
        raise CalibrationError(f"{path} has no usable observations")
    return values


def series_meta(path: Path, values: Mapping[str, float]) -> dict:
    return {
        "rows": len(values),
        "first_observation": min(values),
        "last_observation": max(values),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def to_quarters(values: Mapping[str, float]) -> Dict[int, float]:
    """Aggregate any frequency to quarters by the arithmetic mean of the available months.

    Monthly levels that are annual rates (real PCE, payrolls, unemployment) keep their meaning under
    the mean; a quarterly series has exactly one observation per quarter, so the mean is the value.
    """
    buckets: Dict[int, List[float]] = {}
    for date, value in values.items():
        buckets.setdefault(quarter_index(date), []).append(float(value))
    return {index: math.fsum(bucket) / len(bucket) for index, bucket in buckets.items()}


def load_series(data_root: Path, series_id: str) -> Tuple[Dict[str, float], dict]:
    path = Path(data_root) / series_id / "latest.csv"
    values = read_fred_csv(path)
    try:
        shown = str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        shown = f"<data_root>/{series_id}/latest.csv"
    return values, {"series": series_id, "file": shown, **series_meta(path, values)}


def _window(index_map: Mapping[int, float], start: int, end: int) -> List[Tuple[int, float]]:
    return [(index, value) for index, value in sorted(index_map.items()) if start <= index <= end]


def mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values) if values else float("nan")


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    centre = mean(values)
    return math.sqrt(math.fsum((value - centre) ** 2 for value in values) / (len(values) - 1))


def correlation(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second) or len(first) < 3:
        return float("nan")
    a, b = mean(first), mean(second)
    cov = math.fsum((x - a) * (y - b) for x, y in zip(first, second))
    var_a = math.fsum((x - a) ** 2 for x in first)
    var_b = math.fsum((y - b) ** 2 for y in second)
    if var_a <= 0.0 or var_b <= 0.0:
        return float("nan")
    return cov / math.sqrt(var_a * var_b)


def ols_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 3:
        return float("nan")
    x_mean, y_mean = mean(xs), mean(ys)
    denominator = math.fsum((x - x_mean) ** 2 for x in xs)
    if denominator == 0.0:
        return float("nan")
    return math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator


def _round(value: float, digits: int = 6) -> Optional[float]:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _status(passed: bool, observations: int = 0) -> str:
    """Three-status V2 vocabulary (docs/92): PASS, CONDITIONAL, NOT_APPLICABLE.

    CONDITIONAL is used both for "computed but outside the strict threshold" and for checks that
    are not identities by construction; NOT_APPLICABLE when there are no common observations.
    """
    if observations == 0:
        return "NOT_APPLICABLE"
    return "PASS" if passed else "CONDITIONAL"


# ----------------------------------------------------------------------------------------------
# Calibration
# ----------------------------------------------------------------------------------------------
def _share_block(numerator: Mapping[int, float], denominator: Mapping[int, float],
                 window: Sequence[int]) -> List[float]:
    values = []
    for index in window:
        if index in numerator and index in denominator and denominator[index] != 0.0:
            values.append(numerator[index] / denominator[index])
    return values


def calibrate(data_root: Path = DEFAULT_DATA_ROOT,
              baseline_start: str = DEFAULT_BASELINE_START,
              baseline_end: str = DEFAULT_BASELINE_END,
              recent_start: str = DEFAULT_RECENT_START,
              allow_missing: bool = False) -> dict:
    """Compute the world-core aggregates and the V2 checks; return the report dictionary.

    Raises :class:`CalibrationError` when a required series is absent, unless ``allow_missing`` is
    set - then the affected parameters are reported as UNAVAILABLE and the report status is PARTIAL.
    """
    data_root = Path(data_root)
    raw: Dict[str, Dict[str, float]] = {}
    sources: List[dict] = []
    missing: List[str] = []
    for series_id in REQUIRED + OPTIONAL:
        try:
            values, meta = load_series(data_root, series_id)
        except CalibrationError:
            if series_id in REQUIRED:
                missing.append(series_id)
            continue
        raw[series_id] = values
        sources.append(meta)
    if missing and not allow_missing:
        raise CalibrationError(
            "required series are missing from the data root: " + ", ".join(sorted(missing))
        )

    available = set(raw)
    quarters: Dict[str, Dict[int, float]] = {sid: to_quarters(values)
                                             for sid, values in raw.items()}
    def parse_quarter(label: str) -> int:
        year, tail = label.split("Q", 1)
        return int(year) * 4 + (int(tail) - 1)

    base_start, base_end = parse_quarter(baseline_start), parse_quarter(baseline_end)
    recent = parse_quarter(recent_start) if "Q" in recent_start else None

    gdp = quarters.get("GDPC1", {})
    master = sorted(gdp) if gdp else []
    base_window = [index for index in master if base_start <= index <= base_end]
    recent_window = [index for index in master if recent is not None and index >= recent]
    latest_window = master[-20:] if len(master) >= 20 else master

    parameters: List[dict] = []
    derived: Dict[str, object] = {}

    def add_parameter(parameter: dict) -> None:
        parameters.append(parameter)

    def availability(*ids: str) -> Optional[str]:
        missing_ids = [series_id for series_id in ids if series_id not in available]
        return None if not missing_ids else ", ".join(missing_ids)

    def share_parameter(pid: str, numerator_id: str, denominator_id: str, description: str,
                        windows: Sequence[Tuple[str, Sequence[int]]]) -> None:
        missing_ids = availability(numerator_id, denominator_id)
        if missing_ids:
            add_parameter({"id": pid, "status": "UNAVAILABLE", "missing_series": missing_ids,
                           "unit": "share of real GDP", "description": description})
            return
        numerator, denominator = quarters[numerator_id], quarters[denominator_id]
        block: Dict[str, dict] = {}
        all_values: List[float] = []
        for label, window in windows:
            if not window:
                continue
            values = _share_block(numerator, denominator, window)
            all_values.extend(values)
            block[label] = {
                "quarters": len(values),
                "mean": _round(mean(values)),
                "stdev": _round(stdev(values)),
                "min": _round(min(values)) if values else None,
                "max": _round(max(values)) if values else None,
            }
        primary = block.get("baseline")
        add_parameter({
            "id": pid,
            "status": "CALIBRATED" if primary and primary["mean"] is not None else "UNAVAILABLE",
            "value": primary["mean"] if primary else None,
            "unit": "share of real GDP",
            "description": description,
            "sources": [numerator_id, denominator_id],
            "windows": block,
            "quarters_used": len(all_values),
            "uncertainty": {"stdev_baseline": primary["stdev"] if primary else None,
                            "range_baseline": [primary["min"], primary["max"]] if primary else None},
        })

    share_parameter(
        "gov_share", "GCEC1", "GDPC1",
        "real government consumption expenditures and gross investment (BEA) as a share of real GDP",
        [("baseline", base_window), ("recent", recent_window), ("latest_20_quarters", latest_window)])
    share_parameter(
        "investment_share", "GPDIC1", "GDPC1",
        "real gross private domestic investment (BEA) as a share of real GDP",
        [("baseline", base_window), ("recent", recent_window), ("latest_20_quarters", latest_window)])
    share_parameter(
        "import_share", "IMPGSC1", "GDPC1",
        "real imports of goods and services (BEA) as a share of real GDP (openness starting value)",
        [("baseline", base_window), ("recent", recent_window), ("latest_20_quarters", latest_window)])
    share_parameter(
        "export_share", "EXPGSC1", "GDPC1",
        "real exports of goods and services (BEA) as a share of real GDP (reported, not an independent "
        "parameter - it supports the net-export aggregate)",
        [("baseline", base_window), ("recent", recent_window), ("latest_20_quarters", latest_window)])
    share_parameter(
        "consumption_share", "PCECC96", "GDPC1",
        "real personal consumption expenditures (BEA) as a share of real GDP; the level shares add up "
        "to one only up to the chain-linking residual measured by V2_additivity_levels",
        [("baseline", base_window), ("recent", recent_window), ("latest_20_quarters", latest_window)])

    # NAIRU proxy: mean UNRATE over the last pre-pandemic expansion, monthly observations.
    if "UNRATE" not in available:
        add_parameter({"id": "nairu_proxy", "status": "UNAVAILABLE", "missing_series": "UNRATE",
                       "unit": "percent", "description": "NAIRU proxy from the unemployment rate"})
    else:
        monthly = raw["UNRATE"]
        proxy_start = month_index("2015-01-01")
        proxy_end = month_index("2019-12-31")
        proxy_values = [value for date, value in monthly.items()
                        if proxy_start <= month_index(date) <= proxy_end]
        baseline_start_month = month_index(quarter_start(base_start))
        baseline_end_month = month_index(quarter_start(base_end)) + 2
        baseline_values = [value for date, value in monthly.items()
                           if baseline_start_month <= month_index(date) <= baseline_end_month]
        add_parameter({
            "id": "nairu_proxy",
            "status": "CALIBRATED" if proxy_values else "UNAVAILABLE",
            "value": _round(mean(proxy_values), 4) if proxy_values else None,
            "unit": "percent (unemployment rate)",
            "description": "NAIRU proxy: mean UNRATE over 2015-01..2019-12 (low inflation, sustained "
                           "expansion). This is a proxy for the unemployment rate consistent with "
                           "stable inflation, not an estimate of the natural rate.",
            "sources": ["UNRATE"],
            "windows": {"proxy": {"months": len(proxy_values),
                                  "mean": _round(mean(proxy_values), 4) if proxy_values else None,
                                  "stdev": _round(stdev(proxy_values), 4) if proxy_values else None,
                                  "min": _round(min(proxy_values), 4) if proxy_values else None,
                                  "max": _round(max(proxy_values), 4) if proxy_values else None},
                        "baseline": {"months": len(baseline_values),
                                     "mean": _round(mean(baseline_values), 4) if baseline_values else None}},
            "uncertainty": {"stdev_proxy_window": _round(stdev(proxy_values), 4) if proxy_values else None},
        })

    # Productivity trend: OLS on log(GDPC1 / PAYEMS), annualised.
    if not {"GDPC1", "PAYEMS"} <= available:
        add_parameter({"id": "productivity_trend", "status": "UNAVAILABLE",
                       "missing_series": ", ".join(sorted({"GDPC1", "PAYEMS"} - available)),
                       "unit": "percent per year",
                       "description": "trend growth of real output per employee"})
    else:
        productivity = {index: gdp[index] / quarters["PAYEMS"][index]
                        for index in gdp if index in quarters["PAYEMS"]
                        and quarters["PAYEMS"][index] > 0.0 and gdp[index] > 0.0}

        def trend(window: Sequence[int]) -> dict:
            points = [(index, math.log(productivity[index])) for index in window
                      if index in productivity]
            if len(points) < 8:
                return {"quarters": len(points), "annual_growth_percent": None}
            slope = ols_slope([point[0] for point in points], [point[1] for point in points])
            return {"quarters": len(points),
                    "annual_growth_percent": _round((math.exp(slope * 4.0) - 1.0) * 100.0, 4)}

        pre_2008_end = parse_quarter("2007Q4")
        post_2010_start = parse_quarter("2010Q1")
        blocks = {
            "baseline": trend(base_window),
            "pre_2008": trend([index for index in base_window if index <= pre_2008_end]),
            "post_2010": trend([index for index in base_window if index >= post_2010_start]),
            "latest_20_quarters": trend(latest_window),
        }
        add_parameter({
            "id": "productivity_trend",
            "status": "CALIBRATED" if blocks["baseline"]["annual_growth_percent"] is not None
            else "UNAVAILABLE",
            "value": blocks["baseline"]["annual_growth_percent"],
            "unit": "percent per year (real output per employee)",
            "description": "annualised OLS trend of log(real GDP / total nonfarm payrolls), i.e. real "
                           "output per employee. It is a descriptive trend for the production block, "
                           "not a claim that output is proportional to labour (docs/35).",
            "sources": ["GDPC1", "PAYEMS"],
            "windows": blocks,
            "uncertainty": {"subperiod_spread": _round(
                max((block["annual_growth_percent"] for block in blocks.values()
                     if block["annual_growth_percent"] is not None), default=0.0) -
                min((block["annual_growth_percent"] for block in blocks.values()
                     if block["annual_growth_percent"] is not None), default=0.0), 4)},
        })

    # Derived aggregates (derived statistics only, no raw observations).
    def share_mean(numerator_id: str, denominator_id: str) -> Optional[float]:
        if not {numerator_id, denominator_id} <= available:
            return None
        values = _share_block(quarters[numerator_id], quarters[denominator_id], base_window)
        return _round(mean(values)) if values else None

    derived.update({
        "net_export_share_baseline": (
            _round(share_mean("EXPGSC1", "GDPC1") - share_mean("IMPGSC1", "GDPC1"), 6)
            if share_mean("EXPGSC1", "GDPC1") is not None and share_mean("IMPGSC1", "GDPC1") is not None
            else None),
        "implied_consumption_share_baseline": (
            _round(1.0 - share_mean("GCEC1", "GDPC1") - share_mean("GPDIC1", "GDPC1")
                   - share_mean("EXPGSC1", "GDPC1") + share_mean("IMPGSC1", "GDPC1"), 6)
            if all(share_mean(series_id, "GDPC1") is not None
                   for series_id in ("GCEC1", "GPDIC1", "EXPGSC1", "IMPGSC1"))
            else None),
        "baseline_window": [quarter_label(base_start), quarter_label(base_end)],
        "recent_window": ([quarter_label(recent), quarter_label(master[-1])]
                          if recent is not None and master else None),
        "baseline_quarters": len(base_window),
    })

    v2_checks = _v2_checks(quarters, raw, base_window, available, missing)

    statuses = [parameter["status"] for parameter in parameters]
    report_status = "PARTIAL" if any(status == "UNAVAILABLE" for status in statuses) else "COMPLETE"

    plan_register = _plan_register()
    for source in sources:
        source["register"] = plan_register.get(source["series"])
    content = {
        "parameters": parameters,
        "derived_aggregates": derived,
        "v2_checks": v2_checks,
        "sources": sources,
        "windows": {"baseline": [quarter_label(base_start), quarter_label(base_end)],
                    "recent_from": quarter_label(recent) if recent is not None else None},
        "missing_series": sorted(missing),
    }
    report = {
        "calibration_id": CALIBRATION_ID,
        "version": VERSION,
        "tool": "tools/calibrate_core.py",
        "protocol": "protocols/evaluation_protocol_v4.json",
        "plan": "protocols/data_plan.json (version " + str(_plan_version()) + ")",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": report_status,
        "purpose": "starting values for the world core (docs/79, docs/80): expenditure shares, an "
                   "openness share, a NAIRU proxy and a productivity trend, plus V2 identity checks "
                   "on the real series. Derived statistics only - raw series are not published.",
        "raw_data_committed": False,
        "reproduce": "download the private amfe-data-<run> artifact of the data-refresh workflow "
                     "into data/raw and run: python tools/calibrate_core.py",
        "windows": content["windows"],
        "sources": sources,
        "parameters": parameters,
        "derived_aggregates": derived,
        "v2_checks": v2_checks,
        "criteria": {"v2_thresholds": dict(V2_THRESHOLDS),
                     "window_rule": "baseline = " + DEFAULT_BASELINE_START + ".." + DEFAULT_BASELINE_END
                                   + " (pre-pandemic, 30 years); recent = from " + DEFAULT_RECENT_START,
                     "derived_only": "no raw observations are written to this report"},
        "missing_series": sorted(missing),
        "limitations": [
            "single-country (US) aggregates: the world core needs country blocks; the plan retrieves US "
            "series only, so cross-country aggregation is NOT_APPLICABLE here (see v2_checks)",
            "the shares are ratios of chain-linked real series; they are shares of a 2017-dollar "
            "aggregate, not of nominal GDP, and are not additive across components",
            "the NAIRU proxy is a window mean, not an estimate; the natural rate is unidentified from "
            "these aggregates",
            "the productivity trend is output per employee, not total factor productivity: capital, "
            "hours, composition and utilisation are not in the plan",
            "no structural parameters (elasticities, factor-income shares, behavioural coefficients) "
            "are identified by this calibration",
            "the calibration uses the *latest* tape (data/raw/<series>/latest.csv), not a point-in-time "
            "vintage: it is a starting value, not a REAL_TIME estimate",
        ],
        "next_steps": [
            "add the labour-side series (hours, participation, compensation) and income-side data for "
            "factor shares and a TFP decomposition",
            "repeat the calibration on ALFRED vintages for a PSEUDO_REAL_TIME / REAL_TIME comparison of "
            "the world-core starting values",
            "add non-US country blocks or the cross-country dataset once its licence register entry is "
            "verified",
            "integrate the aggregates into the world-core configuration (docs/79, docs/80) behind an "
            "explicit parameter hash",
        ],
        "content_sha256": _content_sha(content),
    }
    return report


def _content_sha(content: Mapping[str, object]) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _plan_register() -> Dict[str, str]:
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {series["id"]: series.get("register") for series in plan.get("series", [])}


def _plan_version():
    try:
        return json.loads(PLAN.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def _v2_checks(quarters: Mapping[str, Mapping[int, float]],
               raw: Mapping[str, Mapping[str, float]],
               base_window: Sequence[int],
               available: Iterable[str],
               missing: Sequence[str]) -> List[dict]:
    """V2 identity checks on the real series (PASS / CONDITIONAL / NOT_APPLICABLE)."""
    available = set(available)
    checks: List[dict] = []

    def unavailable(check_id: str, name: str, needed: Sequence[str]) -> dict:
        missing_ids = [series_id for series_id in needed if series_id not in available]
        return {"id": check_id, "name": name, "status": "NOT_APPLICABLE",
                "reason": "missing series: " + ", ".join(missing_ids) if missing_ids
                else "missing series"}

    # V2_additivity_levels: Y = C + I + G + NX in chained 2017$ (chain-linked components are not additive).
    needed = ("GDPC1", "PCECC96", "GPDIC1", "GCEC1", "EXPGSC1", "IMPGSC1")
    if not set(needed) <= available:
        checks.append(unavailable("V2_additivity_levels", "Y = C + I + G + NX (levels)", needed))
    else:
        residuals = []
        for index in base_window:
            if index not in quarters["GDPC1"]:
                continue
            gdp = quarters["GDPC1"][index]
            if gdp == 0.0 or any(index not in quarters[series_id] for series_id in needed):
                continue
            components = (quarters["PCECC96"][index] + quarters["GPDIC1"][index]
                          + quarters["GCEC1"][index] + quarters["EXPGSC1"][index]
                          - quarters["IMPGSC1"][index])
            residuals.append((gdp - components) / gdp)
        mean_abs = mean([abs(value) for value in residuals]) if residuals else float("nan")
        max_abs = max((abs(value) for value in residuals), default=float("nan"))
        passed = (math.isfinite(max_abs) and max_abs <= V2_THRESHOLDS["additivity_levels_max"]
                  and mean_abs <= V2_THRESHOLDS["additivity_levels_mean"])
        checks.append({
            "id": "V2_additivity_levels",
            "name": "Y = C + I + G + NX in chained 2017$ (level residual relative to GDP)",
            "status": _status(passed, len(residuals)),
            "statistic": {"mean_abs_residual": _round(mean_abs), "max_abs_residual": _round(max_abs),
                          "quarters": len(residuals)},
            "threshold": {"max_abs_residual": V2_THRESHOLDS["additivity_levels_max"],
                          "mean_abs_residual": V2_THRESHOLDS["additivity_levels_mean"]},
            "note": "chain-linked components are not additive; a small non-zero residual is expected "
                    "and is measured here rather than assumed away",
            "window": [quarter_label(base_window[0]), quarter_label(base_window[-1])] if base_window else None,
        })

    # V2_additivity_growth: first differences scaled by lagged GDP ("growth approximation").
    needed = ("GDPC1", "PCECC96", "GPDIC1", "GCEC1", "EXPGSC1", "IMPGSC1")
    if not set(needed) <= available:
        checks.append(unavailable("V2_additivity_growth", "Y = C + I + G + NX (growth)", needed))
    else:
        residuals = []
        for previous, current in zip(base_window, base_window[1:]):
            if previous not in quarters["GDPC1"] or current not in quarters["GDPC1"]:
                continue
            if any(current not in quarters[series_id] or previous not in quarters[series_id]
                   for series_id in needed):
                continue
            gdp_lag = quarters["GDPC1"][previous]
            if gdp_lag == 0.0:
                continue
            delta_gdp = quarters["GDPC1"][current] - quarters["GDPC1"][previous]
            delta_components = sum(quarters[series_id][current] - quarters[series_id][previous]
                                   for series_id in ("PCECC96", "GPDIC1", "GCEC1", "EXPGSC1"))
            delta_components -= (quarters["IMPGSC1"][current] - quarters["IMPGSC1"][previous])
            residuals.append((delta_gdp - delta_components) / gdp_lag)
        median_abs = sorted(abs(value) for value in residuals)[len(residuals) // 2] if residuals else float("nan")
        max_abs = max((abs(value) for value in residuals), default=float("nan"))
        passed = (math.isfinite(median_abs) and median_abs <= V2_THRESHOLDS["additivity_growth_median"]
                  and max_abs <= V2_THRESHOLDS["additivity_growth_max"])
        checks.append({
            "id": "V2_additivity_growth",
            "name": "Y = C + I + G + NX in first differences scaled by lagged GDP",
            "status": _status(passed, len(residuals)),
            "statistic": {"median_abs_residual": _round(median_abs), "max_abs_residual": _round(max_abs),
                          "quarters": len(residuals)},
            "threshold": {"median_abs_residual": V2_THRESHOLDS["additivity_growth_median"],
                          "max_abs_residual": V2_THRESHOLDS["additivity_growth_max"]},
            "note": "the growth approximation ignores the chain-linking residual and the changing "
                    "weights; the residual statistics are reported, not hidden",
            "window": [quarter_label(base_window[0]), quarter_label(base_window[-1])] if base_window else None,
        })

    # V2_gdp_growth_series: GDPC1-derived annualised q/q growth vs the published A191RL1Q225SBEA.
    if not {"GDPC1", "A191RL1Q225SBEA"} <= available:
        checks.append(unavailable("V2_gdp_growth_series", "published vs derived GDP growth",
                                  ("GDPC1", "A191RL1Q225SBEA")))
    else:
        differences = []
        for index in base_window:
            if index not in quarters["A191RL1Q225SBEA"]:
                continue
            previous = index - 1
            if previous not in quarters["GDPC1"] or index not in quarters["GDPC1"]:
                continue
            gdp_previous = quarters["GDPC1"][previous]
            if gdp_previous <= 0.0:
                continue
            derived = ((quarters["GDPC1"][index] / gdp_previous) ** 4 - 1.0) * 100.0
            differences.append(derived - quarters["A191RL1Q225SBEA"][index])
        mean_abs = mean([abs(value) for value in differences]) if differences else float("nan")
        max_abs = max((abs(value) for value in differences), default=float("nan"))
        passed = (math.isfinite(mean_abs) and mean_abs <= V2_THRESHOLDS["gdp_growth_mean_abs"])
        checks.append({
            "id": "V2_gdp_growth_series",
            "name": "annualised q/q real GDP growth from GDPC1 vs the published A191RL1Q225SBEA",
            "status": _status(passed, len(differences)),
            "statistic": {"mean_abs_difference_pp": _round(mean_abs, 6),
                          "max_abs_difference_pp": _round(max_abs, 6),
                          "quarters": len(differences)},
            "threshold": {"mean_abs_difference_pp": V2_THRESHOLDS["gdp_growth_mean_abs"]},
            "note": "the published growth series is rounded to 0.1 percentage points; agreement at the "
                    "rounding level confirms the deflator/real alignment",
            "window": [quarter_label(base_window[0]), quarter_label(base_window[-1])] if base_window else None,
        })

    # V2_nairu_sanity: the proxy must lie inside the observed baseline range of UNRATE.
    if "UNRATE" not in available:
        checks.append(unavailable("V2_nairu_sanity", "NAIRU proxy sanity", ("UNRATE",)))
    else:
        monthly = raw["UNRATE"]
        values = sorted(monthly.values())
        proxy_start, proxy_end = month_index("2015-01-01"), month_index("2019-12-31")
        proxy = [value for date, value in monthly.items()
                 if proxy_start <= month_index(date) <= proxy_end]
        proxy_mean = mean(proxy) if proxy else float("nan")
        inside = (bool(proxy) and proxy_mean >= values[0] and proxy_mean <= values[-1])
        passed = inside and proxy_mean > values[0]
        checks.append({
            "id": "V2_nairu_sanity",
            "name": "NAIRU proxy lies inside the observed UNRATE range and above its sample minimum",
            "status": _status(passed, len(proxy)),
            "statistic": {"proxy_percent": _round(proxy_mean, 4),
                          "sample_min_percent": _round(values[0], 4),
                          "sample_max_percent": _round(values[-1], 4),
                          "sample_months": len(values)},
            "threshold": "sample_min < proxy_mean <= sample_max",
            "note": "a range check only: the natural rate is not identified by a window mean",
        })

    # V2_productivity_xcheck: GDPC1/PAYEMS vs INDPRO/PAYEMS annual growth correlation.
    if not {"GDPC1", "PAYEMS", "INDPRO"} <= available:
        checks.append(unavailable("V2_productivity_xcheck", "productivity cross-check",
                                  ("GDPC1", "PAYEMS", "INDPRO")))
    else:
        quarters_per_year = 4
        gdp_growth, indpro_growth = [], []
        indexes = list(base_window)
        for index in indexes:
            previous = index - quarters_per_year
            if previous not in quarters["GDPC1"] or previous not in quarters["INDPRO"]:
                continue
            if index not in quarters["PAYEMS"] or previous not in quarters["PAYEMS"]:
                continue
            if quarters["PAYEMS"][previous] <= 0.0 or quarters["PAYEMS"][index] <= 0.0:
                continue
            gdp_growth.append(math.log((quarters["GDPC1"][index] / quarters["PAYEMS"][index])
                                       / (quarters["GDPC1"][previous] / quarters["PAYEMS"][previous])))
            indpro_growth.append(math.log((quarters["INDPRO"][index] / quarters["PAYEMS"][index])
                                          / (quarters["INDPRO"][previous] / quarters["PAYEMS"][previous])))
        corr = correlation(gdp_growth, indpro_growth)
        passed = math.isfinite(corr) and corr >= V2_THRESHOLDS["productivity_corr"]
        checks.append({
            "id": "V2_productivity_xcheck",
            "name": "per-worker real GDP growth vs per-worker industrial production growth",
            "status": _status(passed, len(gdp_growth)),
            "statistic": {"correlation_yoy": _round(corr, 4), "observations": len(gdp_growth)},
            "threshold": {"correlation_yoy": V2_THRESHOLDS["productivity_corr"]},
            "note": "two activity measures, not two estimates of the same quantity: a positive "
                    "association is expected, exact agreement is not",
            "window": [quarter_label(base_window[0]), quarter_label(base_window[-1])] if base_window else None,
        })

    # V2_inflation_measures: CPI vs core PCE vs GDP deflator (CONDITIONAL - different definitions).
    if not {"CPIAUCSL", "PCEPILFE", "GDPDEF"} <= available:
        checks.append(unavailable("V2_inflation_measures", "inflation measures agreement",
                                  ("CPIAUCSL", "PCEPILFE", "GDPDEF")))
    else:
        def yoy(series_id: str, index: int) -> float:
            previous = index - 4
            if previous not in quarters[series_id] or index not in quarters[series_id]:
                return float("nan")
            value = quarters[series_id][previous]
            if value <= 0.0:
                return float("nan")
            return (quarters[series_id][index] / value - 1.0) * 100.0

        pairs = []
        for index in base_window:
            cpi, pce, deflator = yoy("CPIAUCSL", index), yoy("PCEPILFE", index), yoy("GDPDEF", index)
            if all(math.isfinite(value) for value in (cpi, pce, deflator)):
                pairs.append((cpi, pce, deflator))
        cpi_series = [pair[0] for pair in pairs]
        pce_series = [pair[1] for pair in pairs]
        deflator_series = [pair[2] for pair in pairs]
        corr_cpi_pce = correlation(cpi_series, pce_series)
        corr_cpi_deflator = correlation(cpi_series, deflator_series)
        passed = (math.isfinite(corr_cpi_pce) and corr_cpi_pce >= V2_THRESHOLDS["inflation_corr"]
                  and math.isfinite(corr_cpi_deflator)
                  and corr_cpi_deflator >= V2_THRESHOLDS["inflation_corr"])
        checks.append({
            "id": "V2_inflation_measures",
            "name": "CPI vs core PCE vs GDP deflator year-over-year growth",
            "status": _status(passed, len(pairs)),
            "statistic": {"corr_cpi_core_pce": _round(corr_cpi_pce, 4),
                          "corr_cpi_gdp_deflator": _round(corr_cpi_deflator, 4),
                          "quarters": len(pairs)},
            "threshold": {"corr_min": V2_THRESHOLDS["inflation_corr"]},
            "note": "CONDITIONAL by construction: the three measures have different baskets, scopes and "
                    "weights, so a high but imperfect association is the expected result; this is not "
                    "an identity and must not be reported as one",
            "window": [quarter_label(base_window[0]), quarter_label(base_window[-1])] if base_window else None,
        })

    # Explicitly non-identifiable items (their absence is a result, not an omission).
    checks.append({
        "id": "V2_structural_parameters",
        "name": "behavioural elasticities and factor-income shares",
        "status": "NOT_APPLICABLE",
        "reason": "not identifiable from these aggregates: no income-side data (compensation of "
                  "employees, gross operating surplus), no hours/participation series and no "
                  "structural estimation in this protocol run",
    })
    checks.append({
        "id": "V2_world_aggregation",
        "name": "cross-country / world aggregation of the calibrated parameters",
        "status": "NOT_APPLICABLE",
        "reason": "the plan retrieves US series only; country blocks and a world aggregation require "
                  "non-US data whose licence register entries are not verified",
    })
    checks.append({
        "id": "V2_nominal_identity",
        "name": "nominal GDP = real GDP x deflator / 100 as independent evidence",
        "status": "NOT_APPLICABLE",
        "reason": "the implicit GDP deflator is defined as nominal/real, so the identity is "
                  "tautological; the plan has no independent nominal GDP level series. The growth "
                  "consistency check V2_gdp_growth_series is used instead",
    })
    return checks


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate the world-core aggregates from data/raw")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT),
                        help="directory with <SERIES>/latest.csv (git-ignored data/raw)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output JSON path")
    parser.add_argument("--baseline-start", default=DEFAULT_BASELINE_START)
    parser.add_argument("--baseline-end", default=DEFAULT_BASELINE_END)
    parser.add_argument("--allow-missing", action="store_true",
                        help="write a PARTIAL report instead of failing when a series is missing")
    args = parser.parse_args(argv)
    try:
        report = calibrate(Path(args.data_root), args.baseline_start, args.baseline_end,
                           allow_missing=args.allow_missing)
    except CalibrationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                   newline="\n")
    summary = {
        "out": str(out),
        "status": report["status"],
        "parameters": {parameter["id"]: parameter.get("value") for parameter in report["parameters"]},
        "v2": {check["id"]: check["status"] for check in report["v2_checks"]},
        "content_sha256": report["content_sha256"][:16],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
