#!/usr/bin/env python3
"""World-core calibration on the 19-series data plan, with V2 identity/consistency checks.

Extends the stage-2 calibration (``tools/calibrate_core.py``, docs/92) to the expanded plan:
expenditure shares C/I/G/NX (PCECC96, GPDIC1, GCEC1, EXPGSC1, IMPGSC1, GDPC1), real growth
(A191RL1Q225SBEA and a GDPC1-derived cross-check), inflation measures (CPILFESL, PCEPILFE, GDPDEF),
labour market (PAYEMS, UNRATE, NAIRU proxy), industrial production (INDPRO) and retail sales
(RSAFS).  The output feeds the world-core configuration (``src/amfe/world``): normalised
household/government/investment demand shares, real growth, the inflation target, the
unemployment-gap inflation slope and the export share, plus a baseline ``WorldCore`` run digest.

Rules
-----
* only *aggregates* are published: window means, standard deviations, OLS coefficients/standard
  errors, correlations and counts.  Raw series are read from the git-ignored ``data/raw`` and are
  never written into the report; the report stores the SHA-256 of every input file instead;
* the report carries ``content_sha256`` over the deterministic content (without ``generated_at``);
* V2 checks use the three-status vocabulary of docs/92: PASS (criterion met), CONDITIONAL
  (computed but outside the strict threshold, or not an identity by construction), NOT_APPLICABLE
  (cannot be measured with the data plan);
* the calibration is latest-tape, not point-in-time; it provides starting values, not structural
  estimates.  No frozen file and no module of ``src/`` is modified.

Reproduce (local data required, git-ignored):
    python tools/calibrate_world.py --out protocols/world_calibration.json
    python tools/calibrate_world.py --allow-missing --out <tmp>.json   # missing-data mode
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import calibrate_core as core_cal  # noqa: E402  (parsing/aggregation helpers, read-only reuse)

from amfe.world import (CountryConfig, PhysicalConfig, WorldConfig, WorldCore,  # noqa: E402
                        baseline_scenario, canonical_json, default_country_a,
                        default_country_b)

PLAN_PATH = ROOT / "protocols" / "data_plan.json"
CORE_CALIBRATION_PATH = ROOT / "protocols" / "core_calibration.json"
REPORT_PATH = ROOT / "protocols" / "world_calibration.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"

BASELINE_START = "1990Q1"
BASELINE_END = "2019Q4"
RECENT_START = "2020Q1"
RECENT_END = "2026Q3"
NAIRU_WINDOW = ("2015-01-01", "2019-12-31")
LATEST20 = 20
DEFAULT_PERIODS = 8
ROUND = 6

STATUS_PASS = "PASS"
STATUS_CONDITIONAL = "CONDITIONAL"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_PARTIAL = "PARTIAL"

V2_THRESHOLDS = {
    "savings_investment_mean_abs_residual": 0.003,
    "savings_investment_max_abs_residual": 0.010,
    "savings_investment_growth_median_abs_residual": 0.002,
    "savings_investment_growth_max_abs_residual": 0.020,
    "growth_series_max_abs_diff_pp": 0.10,
    "inflation_measures_min_correlation": 0.50,
    "nairu_inflation_min_observations": 40,
    "nairu_inflation_min_abs_t": 2.0,
    "labour_activity_min_correlation": 0.50,
    "industry_activity_min_correlation": 0.45,
    "retail_consumption_min_correlation": 0.50,
    "inflation_persistence_range": [0.30, 0.98],
    "nairu_sanity": "proxy inside the observed UNRATE range and above its minimum",
    "parameters": "point estimates descriptive; windows and dispersion are reported",
}

LIMITATIONS = [
    "latest-tape calibration, not point-in-time: the ALFRED vintage archives are collected but the "
    "starting values here use the latest files (a vintage/PSEUDO_REAL_TIME version is the next step)",
    "only US series are in the plan; the world core's country B keeps its default placeholder "
    "configuration and the cross-country aggregation check is NOT_APPLICABLE",
    "no income-side series (compensation of employees, gross operating surplus), no hours/participation "
    "and no capital stock: the income-expenditure identity, factor shares and TFP cannot be identified",
    "no input-output table and no energy series: the physical layer of the world core keeps its "
    "scenario IO/energy/labour parameters (docs/79, docs/85)",
    "no nominal GDP: the GDP deflator is a real/ nominal ratio by construction, so the nominal identity "
    "check is not independent and is NOT_APPLICABLE",
    "the NAIRU proxy is a 2015-2019 mean of UNRATE, not an estimate of the natural rate; the "
    "Phillips slope is a descriptive unemployment-gap regression, not a structural curve",
    "RSAFS is nominal retail sales while PCECC96 is real consumption: their correlation checks "
    "comovement of activity, not a real identity",
    "the calibrated parameters are starting values for the world core; they are not structural "
    "estimates and the baseline run is a plumb-line, not a forecast",
]

NEXT_STEPS = [
    "repeat the calibration on the dense ALFRED vintage archive (PSEUDO_REAL_TIME) and compare with "
    "the latest-tape values",
    "add income-side and hours/participation series for factor shares and TFP; add capital stock",
    "add an input-output table and energy series for the physical layer of the world core",
    "replace the unemployment-gap inflation slope with a proper output-gap Phillips curve once the "
    "output gap is calibrated",
    "add non-US country blocks (licence-checked) for the world-aggregation check",
]


class CalibrationError(RuntimeError):
    """A required series is missing or a calibration input is invalid."""


# ----------------------------------------------------------------------------------------------
# Calendar helpers (quarter labels -> indices)
# ----------------------------------------------------------------------------------------------
def quarter_index_from_label(label: str) -> int:
    text = str(label).strip().upper()
    if "Q" not in text:
        raise CalibrationError(f"bad quarter label {label!r}; expected e.g. 2019Q4")
    year_text, quarter_text = text.split("Q", 1)
    year, quarter = int(year_text), int(quarter_text)
    if not 1 <= quarter <= 4:
        raise CalibrationError(f"bad quarter {label!r}")
    return year * 4 + (quarter - 1)


def _quarter_map(values) -> dict:
    return core_cal.to_quarters(values)


def _window_indices(start: str, end: str) -> list:
    return list(range(quarter_index_from_label(start), quarter_index_from_label(end) + 1))


def quarter_start_month(index: int) -> int:
    """First month index of a quarter index (compatible with calibrate_core.month_index)."""
    year, quarter = divmod(int(index), 4)
    return year * 12 + quarter * 3


def _window_map(series: dict, start: str, end: str) -> dict:
    start_index, end_index = quarter_index_from_label(start), quarter_index_from_label(end)
    return {index: value for index, value in series.items() if start_index <= index <= end_index}


def _yoy(quarter_map: dict) -> dict:
    result = {}
    for index, value in quarter_map.items():
        previous = quarter_map.get(index - 4)
        if previous is None or previous == 0.0 or not math.isfinite(previous):
            continue
        result[index] = 100.0 * (value / previous - 1.0)
    return result


def _growth(quarter_map: dict) -> dict:
    """Quarter-on-quarter growth in annualised percent (matches the published A191RL definition)."""
    result = {}
    for index, value in quarter_map.items():
        previous = quarter_map.get(index - 1)
        if previous is None or previous <= 0.0 or value is None or value <= 0.0:
            continue
        result[index] = 100.0 * ((value / previous) ** 4 - 1.0)
    return result


def _aligned(*maps) -> dict:
    """Align several index maps on their common indices (values as tuple)."""
    common = set(maps[0])
    for mapping in maps[1:]:
        common &= set(mapping)
    return {index: tuple(mapping[index] for mapping in maps) for index in sorted(common)}


def _mean(values):
    return core_cal.mean(list(values)) if values else float("nan")


def _stdev(values):
    return core_cal.stdev(list(values)) if values else float("nan")


def _round(value, digits: int = ROUND):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _correlation(pairs) -> float:
    if len(pairs) < 3:
        return float("nan")
    first = [pair[0] for pair in pairs]
    second = [pair[1] for pair in pairs]
    return core_cal.correlation(first, second)


def _ols_with_se(xs, ys) -> dict:
    """Simple OLS y = a + b*x with the classical standard error of b."""
    xs, ys = list(xs), list(ys)
    if len(xs) < 3 or len(xs) != len(ys):
        return {"slope": None, "intercept": None, "se": None, "t": None, "observations": len(xs)}
    x_mean, y_mean = core_cal.mean(xs), core_cal.mean(ys)
    sx = math.fsum((x - x_mean) ** 2 for x in xs)
    if sx <= 0.0:
        return {"slope": None, "intercept": None, "se": None, "t": None, "observations": len(xs)}
    slope = math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / sx
    intercept = y_mean - slope * x_mean
    residual = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    dof = len(xs) - 2
    sigma2 = math.fsum(r * r for r in residual) / dof if dof > 0 else float("nan")
    se = math.sqrt(sigma2 / sx) if sigma2 >= 0.0 and dof > 0 else float("nan")
    t_stat = slope / se if se and se > 0.0 else float("nan")
    return {"slope": slope, "intercept": intercept, "se": se,
            "t": t_stat if math.isfinite(t_stat) else None, "observations": len(xs)}


def _status(passed: bool, observations: int) -> str:
    if observations <= 0:
        return STATUS_NOT_APPLICABLE
    return STATUS_PASS if passed else STATUS_CONDITIONAL


def _content_sha(content: dict) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------------------------
def load_plan() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def load_core_calibration() -> dict:
    if not CORE_CALIBRATION_PATH.exists():
        return {}
    return json.loads(CORE_CALIBRATION_PATH.read_text(encoding="utf-8"))


def load_plan_series(data_root: Path, *, allow_missing: bool = False) -> tuple:
    """Load every latest.csv of the plan; return (raw maps, sources, zip-info, missing ids)."""
    plan = load_plan()
    raw, sources, missing = {}, [], []
    plan_entries = {entry["id"]: entry for entry in plan["series"]}
    for series_id in plan_entries:
        try:
            values, meta = core_cal.load_series(Path(data_root), series_id)
        except Exception:
            missing.append(series_id)
            if not allow_missing:
                raise CalibrationError(
                    f"missing data file for {series_id}: {Path(data_root) / series_id / 'latest.csv'}")
            continue
        raw[series_id] = values
        entry = plan_entries[series_id]
        sources.append({
            "series": series_id,
            "file": meta["file"],
            "rows": meta["rows"],
            "first_observation": meta["first_observation"],
            "last_observation": meta["last_observation"],
            "file_sha256": meta["file_sha256"],
            "register": entry.get("register"),
            "licence_label": entry.get("licence_label"),
            "source": entry.get("source"),
            "purpose": entry.get("purpose"),
        })
    return raw, sources, plan, missing


# ----------------------------------------------------------------------------------------------
# Calibration
# ----------------------------------------------------------------------------------------------
class _Builder:
    """Small accumulator for parameters and derived aggregates."""

    def __init__(self, raw: dict, windows: dict):
        self.raw = raw
        self.windows = windows
        self.parameters: list = []
        self.derived: dict = {}

    def available(self, *ids) -> bool:
        return all(series_id in self.raw for series_id in ids)

    def parameter(self, parameter_id: str, description: str, value, *, unit: str,
                  stdev=None, minimum=None, maximum=None, window: str, sources: tuple,
                  target: str | None = None, uncertainty: str = "stdev across quarters",
                  status: str = "CALIBRATED", note: str | None = None) -> None:
        self.parameters.append({
            "id": parameter_id,
            "description": description,
            "value": _round(value),
            "unit": unit,
            "stdev": _round(stdev),
            "min": _round(minimum),
            "max": _round(maximum),
            "window": window,
            "sources": list(sources),
            "uncertainty": uncertainty,
            "status": status,
            "world_core_target": target,
            "note": note,
        })

    def unavailable(self, parameter_id: str, description: str, *, unit: str, sources: tuple,
                    reason: str, target: str | None = None) -> None:
        self.parameters.append({
            "id": parameter_id,
            "description": description,
            "value": None,
            "unit": unit,
            "stdev": None,
            "min": None,
            "max": None,
            "window": None,
            "sources": list(sources),
            "uncertainty": None,
            "status": "NOT_APPLICABLE",
            "world_core_target": target,
            "note": reason,
        })


def _quarterly(raw: dict, series_id: str) -> dict:
    return _quarter_map(raw[series_id]) if series_id in raw else {}


def _share_values(numerator: dict, denominator: dict, window: list) -> list:
    values = []
    for index in window:
        num, den = numerator.get(index), denominator.get(index)
        if num is None or den is None or den == 0.0:
            continue
        values.append(num / den)
    return values


def calibrate(data_root: Path = DEFAULT_DATA_ROOT, *,
              baseline_start: str = BASELINE_START, baseline_end: str = BASELINE_END,
              recent_start: str = RECENT_START, recent_end: str = RECENT_END,
              allow_missing: bool = False, run_world_core: bool = True,
              periods: int = DEFAULT_PERIODS) -> dict:
    data_root = Path(data_root)
    raw, sources, plan, missing = load_plan_series(data_root, allow_missing=allow_missing)
    baseline = _window_indices(baseline_start, baseline_end)
    recent = _window_indices(recent_start, recent_end)
    windows = {"baseline": [baseline_start, baseline_end], "recent": [recent_start, recent_end],
               "latest20": f"last {LATEST20} quarters of the baseline window",
               "nairu_proxy": [NAIRU_WINDOW[0][:7], NAIRU_WINDOW[1][:7]]}
    builder = _Builder(raw, windows)
    q = {series_id: _quarterly(raw, series_id) for series_id in raw}
    checks: list = []

    def add_check(check_id: str, name: str, status: str, statistic: dict, threshold,
                  note: str) -> None:
        checks.append({"id": check_id, "name": name, "status": status,
                       "statistic": statistic, "threshold": threshold, "note": note})

    # --- expenditure shares (real chained dollars 2017) -------------------------------------
    share_specs = [
        ("consumption_share", "PCECC96", "реальное потребление / ВВП",
         "consumption_share (raw share of GDP)"),
        ("investment_share", "GPDIC1", "реальные частные инвестиции / ВВП",
         "investment_share (raw share of GDP)"),
        ("government_share", "GCEC1", "гос. потребление+инвестиции / ВВП",
         "government_share (raw share of GDP)"),
    ]
    raw_shares: dict = {}
    for parameter_id, series_id, description, title in share_specs:
        if not builder.available("GDPC1", series_id):
            builder.unavailable(parameter_id, title, unit="share of GDP", sources=(series_id, "GDPC1"),
                                reason="series missing")
            continue
        values = _share_values(q[series_id], q["GDPC1"], baseline)
        raw_shares[parameter_id] = _mean(values)
        builder.parameter(parameter_id, description, _mean(values),
                          unit="share of GDP", stdev=_stdev(values), minimum=min(values),
                          maximum=max(values), window="baseline", sources=(series_id, "GDPC1"),
                          note=f"{title}; not mapped directly (the normalised shares below are)")
    net_export_share = None
    if builder.available("EXPGSC1", "IMPGSC1", "GDPC1"):
        export_share_values = _share_values(q["EXPGSC1"], q["GDPC1"], baseline)
        import_share_values = _share_values(q["IMPGSC1"], q["GDPC1"], baseline)
        exports = _mean(export_share_values)
        imports = _mean(import_share_values)
        net_values = [e - i for e, i in zip(export_share_values, import_share_values)]
        net_export_share = _mean(net_values)
        builder.parameter("export_share", "реальный экспорт / ВВП", exports, unit="share of GDP",
                          stdev=_stdev(export_share_values), minimum=min(export_share_values),
                          maximum=max(export_share_values), window="baseline",
                          sources=("EXPGSC1", "GDPC1"),
                          target="CountryConfig.export_share",
                          note="external trade share of the calibrated country")
        builder.parameter("import_share", "реальный импорт / ВВП", imports, unit="share of GDP",
                          stdev=_stdev(import_share_values), minimum=min(import_share_values),
                          maximum=max(import_share_values), window="baseline",
                          sources=("IMPGSC1", "GDPC1"),
                          note="openness input; not a separate CountryConfig field")
        builder.parameter("net_export_share", "чистый экспорт / ВВП", net_export_share,
                          unit="share of GDP", stdev=_stdev(net_values), minimum=min(net_values),
                          maximum=max(net_values), window="baseline",
                          sources=("EXPGSC1", "IMPGSC1", "GDPC1"),
                          note="negative NX is why C+I+G exceeds 1; used to normalise the demand shares")
        builder.derived["openness"] = _round(exports + imports)
        builder.derived["net_export_share"] = _round(net_export_share)
        builder.derived["expenditure_sum_share"] = _round(_mean(share_ratio_sum(q, baseline)))
    # normalised demand shares that sum to one (world-core physical layer requirement)
    share_sum = None
    if all(key in raw_shares and raw_shares[key] is not None for key in
           ("consumption_share", "investment_share", "government_share")):
        share_sum = raw_shares["consumption_share"] + raw_shares["investment_share"] + \
            raw_shares["government_share"]
        normalised = {
            "household_demand_share": raw_shares["consumption_share"] / share_sum,
            "investment_demand_share": raw_shares["investment_share"] / share_sum,
            "government_demand_share": raw_shares["government_share"] / share_sum,
        }
        for config_field, value in normalised.items():
            builder.parameter(config_field, f"нормализованная доля (C+I+G=1): {config_field}",
                              value, unit="share of C+I+G", window="baseline",
                              sources=("PCECC96", "GPDIC1", "GCEC1", "GDPC1"),
                              target=f"PhysicalConfig.{config_field}",
                              note="normalised so that the three physical demand shares sum to one")
        builder.derived["demand_shares_sum_before_normalisation"] = _round(share_sum)
        builder.derived["normalised_demand_shares"] = {key: _round(value)
                                                       for key, value in normalised.items()}

    # --- real growth (A191RL) and GDPC1 cross-check ------------------------------------------
    if "A191RL1Q225SBEA" in q:
        growth_window = [index for index in baseline
                         if index in q["A191RL1Q225SBEA"]]
        growth_values = [q["A191RL1Q225SBEA"][index] for index in growth_window]
        annualized_growth = _mean(growth_values)
        builder.parameter("real_growth_annualized", "темп роста реального ВВП, кв/кв annualized",
                          annualized_growth, unit="percent per quarter (annualised)",
                          stdev=_stdev(growth_values), minimum=min(growth_values),
                          maximum=max(growth_values), window="baseline",
                          sources=("A191RL1Q225SBEA",),
                          target="CountryConfig.real_growth (converted to fraction per period)",
                          note="mean published quarterly annualised growth")
        builder.derived["real_growth_per_period_fraction"] = _round(annualized_growth / 4.0 / 100.0)
        builder.derived["real_growth_volatility_pp"] = _round(_stdev(growth_values))
        derived_gdp_growth = _growth(q.get("GDPC1", {}))
        a191_map = {index: q["A191RL1Q225SBEA"][index] for index in growth_window}
        pairs = _aligned(a191_map, derived_gdp_growth)
        if pairs:
            diffs = [abs(first - second) for first, second in pairs.values()]
            mean_diff, max_diff = _mean(diffs), max(diffs)
            status = _status(max_diff <= V2_THRESHOLDS["growth_series_max_abs_diff_pp"], len(diffs))
            add_check("V2_gdp_growth_series",
                      "рост ВВП из GDPC1 (400*ln) vs публикуемый A191RL1Q225SBEA",
                      status,
                      {"mean_abs_diff_pp": _round(mean_diff), "max_abs_diff_pp": _round(max_diff),
                       "quarters": len(diffs)},
                      V2_THRESHOLDS["growth_series_max_abs_diff_pp"],
                      "same BEA source: the difference is the published rounding (0.1 pp)")
    else:
        add_check("V2_gdp_growth_series", "рост ВВП vs публикуемый A191RL1Q225SBEA",
                  STATUS_NOT_APPLICABLE, {}, None, "A191RL1Q225SBEA is missing")

    # --- inflation measures -------------------------------------------------------------------
    inflation_series = {"core_cpi": "CPILFESL", "core_pce": "PCEPILFE", "gdp_deflator": "GDPDEF"}
    yoy = {name: _yoy(q[series_id]) for name, series_id in inflation_series.items()
           if series_id in q}
    inflation_window = [index for index in baseline
                        if all(index in series for series in yoy.values())]
    if len(yoy) >= 2:
        correlations = {}
        names = sorted(yoy)
        for left in range(len(names)):
            for right in range(left + 1, len(names)):
                pairs = [(yoy[names[left]][index], yoy[names[right]][index])
                         for index in inflation_window]
                correlations[f"{names[left]}_{names[right]}"] = _round(_correlation(pairs))
        status = _status(all(correlation is not None and correlation >=
                             V2_THRESHOLDS["inflation_measures_min_correlation"]
                             for correlation in correlations.values()), len(inflation_window))
        add_check("V2_inflation_measures", "согласованность мер инфляции (yoy)",
                  status,
                  {"correlations": correlations, "quarters": len(inflation_window),
                   "means": {name: _round(_mean([yoy[name][index]
                                                 for index in inflation_window]))
                             for name in names}},
                  V2_THRESHOLDS["inflation_measures_min_correlation"],
                  "different baskets are not an identity: the check measures common signal")
        for name in names:
            series_values = [yoy[name][index] for index in inflation_window]
            builder.parameter(f"inflation_{name}_yoy", f"инфляция {name}, yoy",
                              _mean(series_values), unit="percent per year",
                              stdev=_stdev(series_values), minimum=min(series_values),
                              maximum=max(series_values), window="baseline",
                              sources=(inflation_series[name],), uncertainty="stdev across quarters")
        builder.derived["inflation_means_baseline"] = {
            name: _round(_mean([yoy[name][index] for index in inflation_window]))
            for name in names}
        core_pce_2015_2019 = [yoy["core_pce"][index] for index in inflation_window
                              if quarter_index_from_label("2015Q1") <= index <=
                              quarter_index_from_label("2019Q4")]
        if core_pce_2015_2019 and "core_pce" in yoy:
            target_annual = _mean(core_pce_2015_2019)
            builder.parameter("inflation_target", "цель/ориентир инфляции (средняя core PCE 2015–2019)",
                              target_annual / 4.0, unit="percent per period (quarter)",
                              stdev=_stdev(core_pce_2015_2019) / 4.0,
                              window="2015Q1–2019Q4", sources=("PCEPILFE",),
                              target="CountryConfig.inflation_target",
                              note="annualised core PCE divided by four; descriptive anchor, not a policy target")
            builder.derived["inflation_target_annual"] = _round(target_annual)
        # AR(1) persistence of the quarterly core-CPI yoy series (descriptive)
        if "core_cpi" in yoy:
            persistence_pairs = []
            previous = None
            for index in inflation_window:
                value = yoy["core_cpi"][index]
                if previous is not None:
                    persistence_pairs.append((previous, value))
                previous = value
            fit = _ols_with_se([pair[0] for pair in persistence_pairs],
                               [pair[1] for pair in persistence_pairs])
            low, high = V2_THRESHOLDS["inflation_persistence_range"]
            passed = fit["slope"] is not None and low <= fit["slope"] <= high
            add_check("V2_inflation_persistence", "персистентность инфляции, AR(1) core CPI yoy",
                      _status(bool(passed), fit["observations"]),
                      {"ar1": _round(fit["slope"]), "t": _round(fit["t"]),
                       "observations": fit["observations"]},
                      V2_THRESHOLDS["inflation_persistence_range"],
                      "descriptive persistence, not a structural expectation term")

    # --- labour market: NAIRU proxy, employment, NAIRU-inflation link --------------------------
    nairu_proxy = None
    if "UNRATE" in raw:
        monthly = raw["UNRATE"]
        nairu_values = [value for date, value in monthly.items()
                        if NAIRU_WINDOW[0][:7] <= date[:7] <= NAIRU_WINDOW[1][:7]]
        if nairu_values:
            nairu_proxy = _mean(nairu_values)
            builder.parameter("nairu_proxy", "NAIRU-прокси: среднее UNRATE 2015–2019",
                              nairu_proxy, unit="percent", stdev=_stdev(nairu_values),
                              minimum=min(nairu_values), maximum=max(nairu_values),
                              window="2015-01..2019-12", sources=("UNRATE",),
                              note="proxy, not an estimate of the natural rate")
            observed = list(monthly.values())
            start_month = quarter_start_month(quarter_index_from_label(baseline_start))
            end_month = quarter_start_month(quarter_index_from_label(baseline_end)) + 2
            baseline_unrate_values = [value for date, value in monthly.items()
                                      if start_month <= core_cal.month_index(date) <= end_month]
            builder.derived["unrate_baseline_mean"] = _round(_mean(baseline_unrate_values))
            builder.derived["nairu_proxy"] = _round(nairu_proxy)
            passed = min(observed) < nairu_proxy < max(observed)
            add_check("V2_nairu_sanity", "NAIRU-прокси внутри наблюдённого диапазона UNRATE",
                      _status(bool(passed), len(nairu_values)),
                      {"proxy": _round(nairu_proxy), "observed_min": _round(min(observed)),
                       "observed_max": _round(max(observed)), "observations": len(observed)},
                      V2_THRESHOLDS["nairu_sanity"],
                      "sanity check; not identification of the natural rate")
        if nairu_proxy is not None and "core_cpi" in yoy:
            unrate_quarterly = q.get("UNRATE", {})
            pairs = [(unrate_quarterly[index] - nairu_proxy, yoy["core_cpi"][index])
                     for index in inflation_window if index in unrate_quarterly]
            if len(pairs) >= V2_THRESHOLDS["nairu_inflation_min_observations"]:
                fit = _ols_with_se([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                passed = (fit["slope"] is not None and fit["slope"] < 0.0
                          and fit["t"] is not None and fit["t"] <= -V2_THRESHOLDS["nairu_inflation_min_abs_t"])
                add_check("V2_nairu_inflation", "NAIRU-разрыв безработицы и инфляция (Филлипс)",
                          _status(bool(passed), len(pairs)),
                          {"slope_pp_per_pp": _round(fit["slope"]), "se": _round(fit["se"]),
                           "t": _round(fit["t"]), "observations": len(pairs)},
                          {"min_abs_t": V2_THRESHOLDS["nairu_inflation_min_abs_t"],
                           "expected_sign": "negative"},
                          "descriptive unemployment-gap regression, not a structural Phillips curve")
                if fit["slope"] is not None:
                    builder.parameter("inflation_slope", "наклон Филлипса: п.п. инфляции на 1 п.п. разрыва безработицы",
                                      -fit["slope"], unit="percentage points per 1 pp gap",
                                      stdev=fit["se"], window="baseline",
                                      sources=("UNRATE", "CPILFESL"),
                                      target="CountryConfig.inflation_slope",
                                      uncertainty="OLS standard error",
                                      note="mapped as -slope: the config uses a positive output-gap convention "
                                           "while the regression uses an unemployment gap (CONDITIONAL proxy)")
            else:
                add_check("V2_nairu_inflation", "NAIRU-разрыв безработицы и инфляция (Филлипс)",
                          STATUS_NOT_APPLICABLE,
                          {"observations": len(pairs)}, None,
                          "too few common observations for the gap regression")
    if "PAYEMS" in q:
        employment_growth = _yoy(q["PAYEMS"])
        values = [employment_growth[index] for index in baseline if index in employment_growth]
        if values:
            builder.parameter("employment_growth_yoy", "рост занятости (PAYEMS), yoy",
                              _mean(values), unit="percent per year", stdev=_stdev(values),
                              minimum=min(values), maximum=max(values), window="baseline",
                              sources=("PAYEMS",), uncertainty="stdev across quarters")
            builder.derived["employment_growth_mean"] = _round(_mean(values))
        gdp_growth = _growth(q.get("GDPC1", {}))
        pairs = _aligned(employment_growth, gdp_growth)
        window_pairs = [pair for index, pair in pairs.items() if index in set(baseline)]
        correlation = _correlation(window_pairs)
        add_check("V2_labour_activity", "занятость и рост ВВП (yoy)",
                  _status(correlation is not None and math.isfinite(correlation)
                          and abs(correlation) >= V2_THRESHOLDS["labour_activity_min_correlation"],
                          len(window_pairs)),
                  {"correlation": _round(correlation),
                   "quarters": len(window_pairs)},
                  V2_THRESHOLDS["labour_activity_min_correlation"],
                  "comovement of activity, not an identity")

    # --- industrial production and retail sales -------------------------------------------------
    activity_checks = [
        ("V2_industry_activity", "INDPRO", "промышленное производство и рост ВВП",
         V2_THRESHOLDS["industry_activity_min_correlation"]),
        ("V2_retail_consumption", "RSAFS", "розничные продажи и реальное потребление",
         V2_THRESHOLDS["retail_consumption_min_correlation"]),
    ]
    gdp_growth = _growth(q.get("GDPC1", {}))
    consumption_growth = _yoy(q.get("PCECC96", {}))
    for check_id, series_id, name, threshold in activity_checks:
        if series_id not in q:
            add_check(check_id, name, STATUS_NOT_APPLICABLE, {}, None, "series missing")
            continue
        series_growth = _yoy(q[series_id]) if check_id == "V2_industry_activity" else _yoy(q[series_id])
        reference = gdp_growth if check_id == "V2_industry_activity" else consumption_growth
        values = [series_growth[index] for index in baseline if index in series_growth]
        if values:
            builder.parameter(f"{series_id.lower()}_growth_yoy", f"рост {series_id}, yoy",
                              _mean(values), unit="percent per year", stdev=_stdev(values),
                              minimum=min(values), maximum=max(values), window="baseline",
                              sources=(series_id,), uncertainty="stdev across quarters",
                              note="nominal retail series for RSAFS" if series_id == "RSAFS" else None)
            builder.derived[f"{series_id.lower()}_growth_mean"] = _round(_mean(values))
        pairs = _aligned(series_growth, reference)
        window_pairs = [pair for index, pair in pairs.items() if index in set(baseline)]
        correlation = _correlation(window_pairs)
        add_check(check_id, name,
                  _status(correlation is not None and correlation >= threshold, len(window_pairs)),
                  {"correlation": _round(correlation), "quarters": len(window_pairs)},
                  threshold,
                  "comovement check; RSAFS is nominal while PCECC96 is real")

    # --- productivity trend (level and recent) ---------------------------------------------------
    if "GDPC1" in q and "PAYEMS" in q:
        common = sorted(set(q["GDPC1"]) & set(q["PAYEMS"]))
        base_common = [index for index in common if index in set(baseline)]
        if len(base_common) >= 8:
            logs = [math.log(q["GDPC1"][index] / q["PAYEMS"][index]) for index in base_common]
            fit = _ols_with_se(list(range(len(base_common))), logs)
            annual = (math.exp(4.0 * fit["slope"]) - 1.0) * 100.0 if fit["slope"] else None
            builder.parameter("productivity_trend", "тренд выпуска на занятого (GDPC1/PAYEMS)",
                              annual, unit="percent per year",
                              stdev=None, window="baseline",
                              sources=("GDPC1", "PAYEMS"), uncertainty="OLS slope",
                              note="output per employee, not TFP; capital/hours are not in the plan")
            builder.derived["productivity_trend_pp"] = _round(annual)
        latest_common = [index for index in common][-LATEST20:]
        if len(latest_common) >= 8:
            logs = [math.log(q["GDPC1"][index] / q["PAYEMS"][index]) for index in latest_common]
            fit = _ols_with_se(list(range(len(latest_common))), logs)
            annual = (math.exp(4.0 * fit["slope"]) - 1.0) * 100.0 if fit["slope"] else None
            builder.derived["productivity_trend_latest20_pp"] = _round(annual)

    # --- savings-investment (and income-expenditure) checks --------------------------------------
    if all(series_id in q for series_id in ("GDPC1", "PCECC96", "GCEC1", "GPDIC1",
                                            "EXPGSC1", "IMPGSC1")):
        level_pairs, growth_pairs = [], []
        previous = None
        for index in sorted(set(q["GDPC1"]) & set(q["PCECC96"]) & set(q["GCEC1"]) &
                            set(q["GPDIC1"]) & set(q["EXPGSC1"]) & set(q["IMPGSC1"])):
            if index not in set(baseline):
                continue
            gdp = q["GDPC1"][index]
            if gdp == 0.0:
                continue
            income = gdp - q["PCECC96"][index] - q["GCEC1"][index]
            investment = q["GPDIC1"][index] + q["EXPGSC1"][index] - q["IMPGSC1"][index]
            level_pairs.append((index, abs(income - investment) / abs(gdp)))
            if previous is not None and previous[0] == index - 1:
                delta = (gdp - previous[1] - (q["PCECC96"][index] - previous[2])
                         - (q["GCEC1"][index] - previous[3])
                         - (q["GPDIC1"][index] - previous[4])
                         - (q["EXPGSC1"][index] - previous[5])
                         + (q["IMPGSC1"][index] - previous[6]))
                growth_pairs.append(abs(delta) / abs(previous[1]))
            previous = (index, gdp, q["PCECC96"][index], q["GCEC1"][index],
                        q["GPDIC1"][index], q["EXPGSC1"][index], q["IMPGSC1"][index])
        level_residuals = [value for _, value in level_pairs]
        if level_residuals:
            mean_residual, max_residual = _mean(level_residuals), max(level_residuals)
            passed = (mean_residual <= V2_THRESHOLDS["savings_investment_mean_abs_residual"]
                      and max_residual <= V2_THRESHOLDS["savings_investment_max_abs_residual"])
            add_check("V2_savings_investment", "сбережения−инвестиции = чистый экспорт (уровни)",
                      _status(bool(passed), len(level_residuals)),
                      {"mean_abs_residual_over_gdp": _round(mean_residual),
                       "max_abs_residual_over_gdp": _round(max_residual),
                       "quarters": len(level_residuals)},
                      {"mean": V2_THRESHOLDS["savings_investment_mean_abs_residual"],
                       "max": V2_THRESHOLDS["savings_investment_max_abs_residual"]},
                      "identity on chained-dollar series: the residual is the documented chained "
                      "non-additivity, not a data error")
        if growth_pairs:
            median_residual = sorted(growth_pairs)[len(growth_pairs) // 2]
            max_residual = max(growth_pairs)
            passed = (median_residual <=
                      V2_THRESHOLDS["savings_investment_growth_median_abs_residual"]
                      and max_residual <=
                      V2_THRESHOLDS["savings_investment_growth_max_abs_residual"])
            add_check("V2_savings_investment_growth", "сбережения−инвестиции, первые разности",
                      _status(bool(passed), len(growth_pairs)),
                      {"median_abs_residual": _round(median_residual),
                       "max_abs_residual": _round(max_residual),
                       "quarters": len(growth_pairs)},
                      {"median": V2_THRESHOLDS["savings_investment_growth_median_abs_residual"],
                       "max": V2_THRESHOLDS["savings_investment_growth_max_abs_residual"]},
                      "growth approximation of the same identity; should hold tightly")
    else:
        add_check("V2_savings_investment", "сбережения−инвестиции = чистый экспорт",
                  STATUS_NOT_APPLICABLE, {}, None, "required expenditure series missing")

    add_check("V2_income_expenditure",
              "доходы−расходы (доходный подход к ВВП)",
              STATUS_NOT_APPLICABLE, {},
              None,
              "no income-side series (compensation of employees, gross operating surplus, taxes) "
              "in the 19-series plan; the expenditure identity is covered by V2_savings_investment")
    add_check("V2_nominal_identity", "номинальный ВВП = реальный × дефлятор",
              STATUS_NOT_APPLICABLE, {}, None,
              "the deflator is defined as nominal/real; no independent nominal GDP level is in the plan")
    add_check("V2_structural_parameters", "поведенческие эластичности и факторные доли",
              STATUS_NOT_APPLICABLE, {}, None,
              "needs income-side series, hours/participation and a structural identification")
    add_check("V2_capital_energy_io", "капитал, энергия и IO-таблица",
              STATUS_NOT_APPLICABLE, {}, None,
              "the plan has no capital stock, energy or input-output table; the physical layer of the "
              "world core keeps scenario parameters (docs/79)")
    add_check("V2_world_aggregation", "агрегирование параметров по странам/миру",
              STATUS_NOT_APPLICABLE, {}, None,
              "the plan contains only US series; country B of the world core keeps its default placeholder")

    status = STATUS_PARTIAL if missing else "COMPLETE"
    parameters = _finalise_parameters(builder.parameters)
    world_core = _world_core_baseline(parameters, run=run_world_core, periods=periods,
                                      missing=missing)
    content = {
        "calibration_id": "AMFE-WORLD-CALIBRATION",
        "version": "0.1.0",
        "tool": "tools/calibrate_world.py",
        "plan": {"file": "protocols/data_plan.json", "version": plan.get("version"),
                 "series": len(plan["series"])},
        "core_calibration": {
            "file": "protocols/core_calibration.json",
            "version": load_core_calibration().get("version"),
        },
        "status": status,
        "purpose": (
            "starting values for the world core (src/amfe/world, docs/85): normalised expenditure "
            "shares, openness, growth, inflation anchor, unemployment-gap inflation slope, NAIRU proxy "
            "and activity aggregates, plus V2 checks on the real series; derived statistics only"),
        "raw_data_committed": False,
        "reproduce": ("put the local data/raw download in place (data is git-ignored) and run "
                      "python tools/calibrate_world.py"),
        "windows": windows,
        "sources": sources,
        "parameters": parameters,
        "derived_aggregates": builder.derived,
        "v2_checks": checks,
        "criteria": {"statuses": {STATUS_PASS: "criterion met",
                                  STATUS_CONDITIONAL: "computed but outside the strict threshold, "
                                                      "or not an identity by construction",
                                  STATUS_NOT_APPLICABLE: "cannot be measured with the plan"},
                     "v2_thresholds": V2_THRESHOLDS},
        "world_core": world_core,
        "missing_series": missing,
        "limitations": list(LIMITATIONS),
        "next_steps": list(NEXT_STEPS),
    }
    content["content_sha256"] = _content_sha(content)
    return content


def share_ratio_sum(q: dict, window: list):
    values = []
    for index in window:
        if all(series in q for series in ("PCECC96", "GPDIC1", "GCEC1", "GDPC1")):
            gdp = q["GDPC1"].get(index)
            if gdp:
                values.append((q["PCECC96"][index] + q["GPDIC1"][index] + q["GCEC1"][index]) / gdp)
    return values


def _finalise_parameters(parameters: list) -> list:
    """Add the world-core mapping value for each target, keeping the report easy to audit."""
    lookup = {parameter["id"]: parameter["value"] for parameter in parameters}
    for parameter in parameters:
        target = parameter.get("world_core_target")
        if target and target.startswith("PhysicalConfig."):
            field = target.split(".", 1)[1]
            parameter["world_core_value"] = lookup.get(field)
        elif target and target.startswith("CountryConfig.real_growth"):
            fraction = (lookup.get("real_growth_annualized") or 0.0) / 4.0 / 100.0
            parameter["world_core_value"] = _round(fraction)
        elif target and target == "CountryConfig.inflation_target":
            parameter["world_core_value"] = parameter["value"]
        elif target and target == "CountryConfig.inflation_slope":
            parameter["world_core_value"] = (parameter["value"]
                                             if parameter["value"] is not None
                                             and parameter["value"] > 0.0 else None)
            if parameter["world_core_value"] is None:
                parameter["note"] = ((parameter.get("note") or "") +
                                     "; negative/sign-unexpected slope is not mapped into the config")
        elif target and target == "CountryConfig.export_share":
            parameter["world_core_value"] = parameter["value"]
        else:
            parameter["world_core_value"] = None
    return parameters


def _parameter_value(parameters: list, parameter_id: str):
    for parameter in parameters:
        if parameter["id"] == parameter_id:
            return parameter["value"]
    return None


def build_world_config(parameters: list) -> WorldConfig | None:
    """WorldConfig with the calibrated country A; country B stays the default placeholder."""
    household = _parameter_value(parameters, "household_demand_share")
    investment = _parameter_value(parameters, "investment_demand_share")
    government = _parameter_value(parameters, "government_demand_share")
    if None in (household, investment, government):
        return None
    total = household + investment + government
    if total <= 0.0:
        raise CalibrationError("demand shares must sum to a positive number")
    # renormalise the rounded reported values so the dataclass invariant holds exactly
    household, investment, government = household / total, investment / total, government / total
    if abs(household + investment + government - 1.0) > 1e-6:
        raise CalibrationError("demand shares must sum to one")
    real_growth = _parameter_value(parameters, "real_growth_annualized")
    inflation_target = _parameter_value(parameters, "inflation_target")
    inflation_slope = _parameter_value(parameters, "inflation_slope")
    export_share = _parameter_value(parameters, "export_share")
    physical = PhysicalConfig(household_demand_share=household,
                              government_demand_share=government,
                              investment_demand_share=investment)
    kwargs = {"physical": physical}
    if real_growth is not None:
        kwargs["real_growth"] = real_growth / 4.0 / 100.0
    if inflation_target is not None:
        kwargs["inflation_target"] = inflation_target
    if inflation_slope is not None and inflation_slope > 0.0:
        kwargs["inflation_slope"] = inflation_slope
    if export_share is not None:
        kwargs["export_share"] = export_share
    country_a = replace_country(default_country_a(), **kwargs)
    return WorldConfig(country_a=country_a, country_b=default_country_b())


def replace_country(country: CountryConfig, **changes) -> CountryConfig:
    """Dataclass replace helper kept explicit so tests can see exactly what changes."""
    from dataclasses import replace

    return replace(country, **changes)


def world_config_payload(config: WorldConfig) -> dict:
    from dataclasses import asdict

    return asdict(config)


def world_config_sha(config: WorldConfig) -> str:
    return hashlib.sha256(canonical_json(world_config_payload(config)).encode("utf-8")).hexdigest()


def _world_core_baseline(parameters: list, *, run: bool, periods: int,
                         missing: list) -> dict:
    if missing:
        return {"status": STATUS_NOT_APPLICABLE,
                "reason": f"missing series prevent the world-core configuration: {missing}"}
    try:
        config = build_world_config(parameters)
    except CalibrationError as exc:
        return {"status": STATUS_NOT_APPLICABLE, "reason": str(exc)}
    if config is None:
        return {"status": STATUS_NOT_APPLICABLE,
                "reason": "normalised expenditure shares are unavailable"}
    payload = {
        "status": "COMPLETE" if run else "SKIPPED",
        "calibrated_country": "country_a",
        "country_b": "default placeholder (no non-US data in the plan)",
        "config_sha256": world_config_sha(config),
        "config": world_config_payload(config),
    }
    if not run:
        return payload
    try:
        core = WorldCore(config, baseline_scenario())
        result = core.run(periods=int(periods))
    except Exception as exc:  # pragma: no cover - integration failure is reported, not raised
        payload.update({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
        return payload
    payload.update({
        "periods": int(periods),
        "digest": result.digest,
        "ok": bool(result.ok),
        "all_periods_ok": all(report.ok for report in result.reports),
        "periods_reported": len(result.reports),
        "note": "deterministic baseline run with the calibrated country A; a plumb-line digest, "
                "not a forecast",
    })
    return payload


def load_world_config(report: dict) -> WorldConfig | None:
    """Rebuild the WorldConfig from the stored payload (used by tests and future integration)."""
    payload = (report.get("world_core") or {}).get("config")
    if not payload:
        return None
    country_a = payload["country_a"]
    country_b = payload["country_b"]
    physical = PhysicalConfig(**country_a["physical"])
    country = CountryConfig(**{**country_a, "physical": physical})
    other = CountryConfig(**{**country_b, "physical": PhysicalConfig(**country_b["physical"])})
    return WorldConfig(**{**payload, "country_a": country, "country_b": other})


# ----------------------------------------------------------------------------------------------
# Hygiene and CLI
# ----------------------------------------------------------------------------------------------
FORBIDDEN_KEYS = {"raw", "raw_values", "raw_series", "series_values", "time_series",
                  "observations_values", "raw_data_points"}
MAX_AGGREGATE_LIST = 40


def find_raw_values(report, path: str = "report") -> list:
    """Paths of values that must not appear in the published report (raw series etc.)."""
    problems: list = []
    if isinstance(report, dict):
        for key, value in report.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                problems.append(f"{path}.{key}: forbidden key")
            problems.extend(find_raw_values(value, f"{path}.{key}"))
    elif isinstance(report, list):
        if len(report) > MAX_AGGREGATE_LIST and all(
                isinstance(item, (int, float)) for item in report):
            problems.append(f"{path}: numeric list of length {len(report)}")
        for index, value in enumerate(report):
            problems.extend(find_raw_values(value, f"{path}[{index}]"))
    return problems


def write_report(report: dict, path: Path | str = REPORT_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return target


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def summary_lines(report: dict) -> list:
    lines = [f"status: {report['status']} (missing: {report.get('missing_series') or 'none'})"]
    for parameter in report["parameters"]:
        value = parameter["value"]
        lines.append(f"  {parameter['id']}: {value} {parameter['unit']}"
                     if value is not None else f"  {parameter['id']}: NOT_APPLICABLE")
    for check in report["v2_checks"]:
        lines.append(f"  {check['id']}: {check['status']}")
    world = report.get("world_core", {})
    if world.get("digest"):
        lines.append(f"  world-core baseline digest: {world['digest']}")
    lines.append(f"  content_sha256: {report['content_sha256']}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="World-core calibration on the 19-series plan")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--baseline-start", default=BASELINE_START)
    parser.add_argument("--baseline-end", default=BASELINE_END)
    parser.add_argument("--allow-missing", action="store_true",
                        help="report PARTIAL instead of failing when series are absent")
    parser.add_argument("--skip-world-core", action="store_true",
                        help="do not run the WorldCore baseline (diagnostics only)")
    parser.add_argument("--periods", type=int, default=DEFAULT_PERIODS)
    parser.add_argument("--out", default=str(REPORT_PATH))
    parser.add_argument("--no-timestamp", action="store_true")
    args = parser.parse_args(argv)
    report = calibrate(Path(args.data_root), baseline_start=args.baseline_start,
                       baseline_end=args.baseline_end, allow_missing=args.allow_missing,
                       run_world_core=not args.skip_world_core, periods=args.periods)
    report = _clean(report)
    if not args.no_timestamp:
        report["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    problems = find_raw_values(report)
    if problems:
        raise SystemExit("report contains raw data: " + "; ".join(problems[:5]))
    path = write_report(report, args.out)
    status_counts: dict = {}
    for check in report["v2_checks"]:
        status_counts[check["status"]] = status_counts.get(check["status"], 0) + 1
    print(json.dumps({"status": report["status"], "out": str(path),
                      "parameters": len(report["parameters"]),
                      "checks": status_counts}, ensure_ascii=False))
    for line in summary_lines(report):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
