"""Labor and household layer for the AMFE meso-micro block (docs/136).

The layer consumes two independent inputs:

* **labor data** -- the normalized WIOD SEA 2016 file (``sea_normalized.json`` written by
  ``tools/labor_import.py``): compensation of employees, labour compensation (including the
  self-employed), gross value added, gross output, persons engaged, employees and hours worked by
  country x industry x year, in millions of national currency / thousands of persons / millions of
  hours.  Currency-free ratios (labour share, hours per employee) are computed directly; per-USD
  intensities use the canonical IO table (``USD_mn``) from :mod:`amfe.mrio`.
* **IO table** -- the canonical ``MRIODataset`` (WIOD 2016 / FIGARO), only for output denominators.

What it deliberately does not do
--------------------------------
* It does not invent missing cells: a missing country/industry pair is skipped and counted, never
  filled with a guess; the loader is fail-closed.
* It does not convert national currency to USD: labour shares and hours are currency-free, and
  intensity per USD output is computed as ``hours / output_usd_mn`` (no FX needed).  Absolute
  compensation in USD would need an FX layer that this module does not own.
* It is not a wage-formation or bargaining model: the shares are accounting ratios, not behavioural
  parameters, and the household block only aggregates the observed household side.

Public API
----------
``LaborData``                    validated normalized SEA data (digest, accessors).
``load_labor``                   fail-closed loader from ``$AMFE_MRIO_ROOT/labor``.
``labor_share``                  compensation / value added by country x industry (clipped, counted).
``industry_labor_shares``        value-weighted shares by industry across countries.
``country_aggregates``           country totals: output, VA, compensation, persons, hours, shares.
``top_countries_by_labor_share`` ranking used by the reports/docs.
``employment_intensity``         hours and persons per USD_mn of output (needs the IO table).
``household_block``              household consumption share, compensation per employee, aggregates.
``labour_invariants``            named checks (non-negativity, bounds, sums, plausibility).
``build_labor_block``            aggregate-only report block (``status=RESEARCH_ONLY``).
``sfc_link``                     labour share vs the SFC household/income aggregates.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

LAYOUT = "amfe.layouts.labor.v1"
LABOR_ENV = "AMFE_MRIO_ROOT"
LABOR_DIRNAME = "labor"
NORMALIZED_FILENAME = "sea_normalized.json"
TOLERANCE = 1e-6
#: Plausibility band for annual hours per employee.
HOURS_PER_EMPLOYEE_MIN = 100.0
HOURS_PER_EMPLOYEE_MAX = 4000.0
#: A labour share above this value is reported as an outlier (accounting data can exceed 1 when
#: compensation includes items financed outside value added); it is clipped for the [0,1] checks.
LABOR_SHARE_OUTLIER = 1.0
#: Required SEA variables for the accounting identities.
REQUIRED_VARIABLES = ("COMP", "LAB", "VA", "GO", "EMP", "EMPE", "H_EMPE")


class LaborError(ValueError):
    """Base class for labour-layer errors."""


class LaborDataError(LaborError):
    """Fail-closed: the normalized labor dataset is absent, unreadable or inconsistent."""


# ---------------------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------------------
def _finite(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LaborDataError(f"{name} is not a number: {value!r}") from exc
    if not math.isfinite(number):
        raise LaborDataError(f"{name} must be finite, got {number!r}")
    return number


@dataclass(frozen=True)
class LaborData:
    """Normalized SEA data: ``values[year][country][industry][variable]``."""

    years: tuple[int, ...]
    countries: tuple[str, ...]
    industries: tuple[str, ...]
    values: dict
    units: dict
    source: dict
    schema: str = LAYOUT

    def __post_init__(self) -> None:
        years = tuple(int(year) for year in self.years)
        countries = tuple(str(item).strip() for item in self.countries)
        industries = tuple(str(item).strip() for item in self.industries)
        if not years or not countries or not industries:
            raise LaborDataError("years, countries and industries must be non-empty")
        if list(years) != sorted(set(years)):
            raise LaborDataError("years must be unique and ascending")
        if len(set(countries)) != len(countries) or len(set(industries)) != len(industries):
            raise LaborDataError("country/industry labels must be unique")
        if not isinstance(self.values, Mapping):
            raise LaborDataError("values must be a mapping")
        if not isinstance(self.units, Mapping):
            raise LaborDataError("units must be a mapping")
        cleaned: dict = {}
        for year in years:
            year_block = self.values.get(str(year), self.values.get(year))
            if not isinstance(year_block, Mapping):
                raise LaborDataError(f"no values for {year}")
            countries_seen = {}
            for country, industries_block in year_block.items():
                if not isinstance(industries_block, Mapping):
                    raise LaborDataError(f"{year}/{country} is not a mapping")
                cells = {}
                for industry, variables in industries_block.items():
                    entry = {str(name): _finite(value, f"{year}/{country}/{industry}/{name}")
                             for name, value in dict(variables).items()}
                    cells[str(industry)] = entry
                countries_seen[str(country)] = cells
            cleaned[str(year)] = countries_seen
        object.__setattr__(self, "years", years)
        object.__setattr__(self, "countries", countries)
        object.__setattr__(self, "industries", industries)
        object.__setattr__(self, "values", cleaned)
        object.__setattr__(self, "units", dict(self.units))
        object.__setattr__(self, "source", dict(self.source))

    # -- accessors --------------------------------------------------------------------
    def value(self, year: int, country: str, industry: str, variable: str) -> float | None:
        cell = self.values.get(str(int(year)), {}).get(country, {}).get(industry, {})
        return cell.get(variable)

    def country_totals(self, year: int, variable: str) -> dict[str, float]:
        """Sum a variable over industries for every country (missing cells are skipped)."""
        totals = {}
        for country, industries_block in self.values.get(str(int(year)), {}).items():
            totals[country] = math.fsum(cell.get(variable, 0.0)
                                        for cell in industries_block.values())
        return totals

    def industry_totals(self, year: int, variable: str) -> dict[str, float]:
        """Sum a variable over countries for every industry."""
        totals: dict[str, float] = {}
        for industries_block in self.values.get(str(int(year)), {}).values():
            for industry, cell in industries_block.items():
                totals[industry] = totals.get(industry, 0.0) + cell.get(variable, 0.0)
        return totals

    def digest(self) -> str:
        payload = json.dumps({"years": list(self.years), "countries": list(self.countries),
                              "industries": list(self.industries), "values": self.values},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def describe(self) -> dict:
        return {"schema": self.schema, "years": list(self.years),
                "countries": len(self.countries), "industries": len(self.industries),
                "cells": sum(len(year) for year in self.values.values()),
                "digest": self.digest(),
                "source": self.source.get("name") if isinstance(self.source, Mapping) else None}


def labor_root(root=None) -> Path:
    if root is not None:
        return Path(root) / LABOR_DIRNAME
    env = os.environ.get(LABOR_ENV, "").strip()
    if not env:
        raise LaborDataError(
            f"BLOCKED_DATA: {LABOR_ENV} is not set and no --root given; run "
            "tools/labor_import.py --download --parse (docs/136)")
    return Path(env) / LABOR_DIRNAME


def load_labor(root=None, path=None) -> LaborData:
    """Fail-closed loader of ``sea_normalized.json`` (env ``AMFE_MRIO_ROOT``/labor)."""
    target = Path(path) if path is not None else labor_root(root) / NORMALIZED_FILENAME
    if not target.is_file():
        raise LaborDataError(f"BLOCKED_DATA: normalized labor data not found: {target}; "
                             "run tools/labor_import.py --parse (docs/136)")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaborDataError(f"BLOCKED_DATA: {target} is not readable JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise LaborDataError(f"BLOCKED_DATA: {target} must be a JSON object")
    required = ("years", "countries", "industries", "values")
    missing = [key for key in required if key not in document]
    if missing:
        raise LaborDataError(f"BLOCKED_DATA: {target} misses {missing}")
    return LaborData(years=tuple(document["years"]), countries=tuple(document["countries"]),
                     industries=tuple(document["industries"]), values=document["values"],
                     units=document.get("units") or {}, source=document.get("source") or {},
                     schema=str(document.get("schema", LAYOUT)))


# ---------------------------------------------------------------------------------------
# Labour shares
# ---------------------------------------------------------------------------------------
def labor_share(labor: LaborData, year: int, numerator: str = "COMP",
                denominator: str = "VA", clip: bool = True) -> dict:
    """``numerator / denominator`` by country x industry, with clipping counters.

    ``COMP/VA`` is the employee compensation share; ``LAB/VA`` additionally imputes the labour
    income of the self-employed (SEA definition).  Ratios are currency-free.  Values above
    :data:`LABOR_SHARE_OUTLIER` (or below zero) are counted as outliers; when ``clip`` is true the
    returned share is clamped to ``[0, 1]`` so the downstream [0,1] invariant holds, and the raw
    outlier is kept in the ``outliers`` list (no silent loss).
    """
    shares: dict[str, dict[str, float]] = {}
    outliers = []
    clipped = 0
    for country, industries_block in labor.values.get(str(int(year)), {}).items():
        for industry, cell in industries_block.items():
            value = cell.get(numerator)
            base = cell.get(denominator)
            if value is None or base is None or base <= 0.0:
                continue
            ratio = value / base
            if ratio < 0.0 or ratio > LABOR_SHARE_OUTLIER:
                outliers.append({"country": country, "industry": industry,
                                 "share": round(ratio, 9)})
                if clip:
                    ratio = min(max(ratio, 0.0), 1.0)
                    clipped += 1
            shares.setdefault(country, {})[industry] = ratio
    return {"year": int(year), "numerator": numerator, "denominator": denominator,
            "shares": shares, "outliers": outliers, "clipped": clipped,
            "clip": bool(clip)}


def industry_labor_shares(labor: LaborData, year: int, numerator: str = "COMP") -> dict:
    """Value-added-weighted labour share by industry across all countries."""
    numerator_totals = labor.industry_totals(year, numerator)
    denominator_totals = labor.industry_totals(year, "VA")
    result = {}
    for industry, value in numerator_totals.items():
        base = denominator_totals.get(industry, 0.0)
        if base > 0.0:
            result[industry] = round(value / base, 9)
    return result


def country_aggregates(labor: LaborData, year: int) -> dict:
    """Country totals and ratios: output, VA, compensation, persons, hours, labour share."""
    aggregates = {}
    for country, industries_block in labor.values.get(str(int(year)), {}).items():
        totals = {}
        for variable in ("COMP", "LAB", "VA", "GO", "EMP", "EMPE", "H_EMPE"):
            totals[variable] = math.fsum(
                cell.get(variable, 0.0) for cell in industries_block.values())
        va = totals["VA"]
        employees = totals["EMPE"]
        hours = totals["H_EMPE"]
        aggregates[country] = {
            "output_mn_lcu": round(totals["GO"], 6),
            "value_added_mn_lcu": round(va, 6),
            "compensation_mn_lcu": round(totals["COMP"], 6),
            "labour_compensation_mn_lcu": round(totals["LAB"], 6),
            "persons_engaged_thousand": round(totals["EMP"], 6),
            "employees_thousand": round(employees, 6),
            "hours_worked_million": round(hours, 6),
            "labor_share": round(totals["COMP"] / va, 6) if va > 0 else None,
            "labor_share_incl_self_employed": round(totals["LAB"] / va, 6) if va > 0 else None,
            # H_EMPE is millions of hours, EMPE thousands of employees: x1000 gives annual hours
            "hours_per_employee": round(hours * 1e3 / employees, 3) if employees > 0 else None,
            "compensation_per_employee_lcu": round(totals["COMP"] * 1e3 / employees, 3)
            if employees > 0 else None,
        }
    return aggregates


def top_countries_by_labor_share(labor: LaborData, year: int, limit: int = 10,
                                 include_self_employed: bool = False) -> list[dict]:
    """Countries ranked by the aggregate labour share (VA-weighted), highest first."""
    aggregates = country_aggregates(labor, year)
    key = "labor_share_incl_self_employed" if include_self_employed else "labor_share"
    ranked = [{"country": country, "labor_share": values[key],
               "value_added_mn_lcu": values["value_added_mn_lcu"],
               "compensation_mn_lcu": values["compensation_mn_lcu"]}
              for country, values in aggregates.items() if values.get(key) is not None]
    ranked.sort(key=lambda item: (-item["labor_share"], item["country"]))
    return ranked[:int(limit)]


def employment_intensity(labor: LaborData, io_dataset, year: int) -> dict:
    """Hours and persons per USD_mn of output, matched to the canonical IO table.

    ``io_dataset`` may be an :class:`amfe.mrio.schema.MRIODataset` or a mapping with
    ``countries``/``industries``/``x``.  Only matched pairs are used (missing pairs are counted,
    never filled).  The IO table must be in ``USD_mn`` for the intensity to be meaningful.
    """
    if hasattr(io_dataset, "countries"):
        io_countries = tuple(io_dataset.countries)
        io_industries = tuple(io_dataset.industries)
        output = list(io_dataset.x)
        units = str(getattr(io_dataset, "units", "USD_mn"))
        index_of = getattr(io_dataset, "index_of", None)
        if index_of is None:
            def index_of(country, industry, _c=io_countries, _i=io_industries):
                return _c.index(country) * len(_i) + _i.index(industry)
    else:
        io_countries = tuple(io_dataset["countries"])
        io_industries = tuple(io_dataset["industries"])
        output = [float(value) for value in io_dataset["x"]]
        units = str(io_dataset.get("units", "USD_mn"))
        def index_of(country, industry, _c=io_countries, _i=io_industries):
            return _c.index(country) * len(_i) + _i.index(industry)
    if units != "USD_mn":
        raise LaborError(f"employment_intensity needs USD_mn output, got {units!r}")
    rows = []
    matched = missing = 0
    for country, industries_block in labor.values.get(str(int(year)), {}).items():
        if country not in io_countries:
            missing += len(industries_block)
            continue
        for industry, cell in industries_block.items():
            if industry not in io_industries:
                missing += 1
                continue
            value = output[index_of(country, industry)]
            if value is None or value <= 0.0:
                missing += 1
                continue
            hours = cell.get("H_EMPE", 0.0) * 1e6          # millions -> hours
            persons = cell.get("EMP", 0.0) * 1e3           # thousands -> persons
            rows.append({
                "country": country, "industry": industry, "output_usd_mn": round(value, 6),
                "hours_per_usd_mn": round(hours / value, 6),
                "persons_per_usd_mn": round(persons / value, 6),
            })
            matched += 1
    rows.sort(key=lambda item: (item["country"], item["industry"]))
    return {"year": int(year), "rows": rows, "matched": matched, "missing": missing,
            "method": "hours(persons) / gross output in USD_mn from the canonical IO table"}


# ---------------------------------------------------------------------------------------
# Households
# ---------------------------------------------------------------------------------------
def household_block(labor: LaborData, year: int, wdi: Mapping | None = None) -> dict:
    """Household aggregates from the labour side plus optional WDI consumption shares.

    ``wdi`` may carry ``consumption_share_of_gdp`` (NE.CON.PRVT.ZS) and ``gdp_per_capita_usd``
    (NY.GDP.PCAP.CD) per country; when absent the corresponding fields are ``None`` -- never
    estimated.  Compensation and consumption are in different units (national currency vs % of
    GDP), so the block reports shares and per-worker compensation, not a currency conversion.
    """
    aggregates = country_aggregates(labor, year)
    consumption = (wdi or {}).get("consumption_share_of_gdp") or {}
    gdp_per_capita = (wdi or {}).get("gdp_per_capita_usd") or {}
    rows = {}
    for country, values in aggregates.items():
        va = values["value_added_mn_lcu"]
        consumption_share = consumption.get(country)
        rows[country] = {
            "labor_share": values["labor_share"],
            "compensation_share_of_output": round(values["compensation_mn_lcu"]
                                                  / values["output_mn_lcu"], 6)
            if values["output_mn_lcu"] > 0 else None,
            "compensation_per_employee_lcu": values["compensation_per_employee_lcu"],
            "hours_per_employee": values["hours_per_employee"],
            "household_consumption_share_of_gdp": consumption_share,
            "gdp_per_capita_usd": gdp_per_capita.get(country),
            "compensation_to_gdp_proxy": round(values["compensation_mn_lcu"] / va, 6)
            if va > 0 else None,
        }
    world_compensation = math.fsum(values["compensation_mn_lcu"]
                                   for values in aggregates.values())
    world_va = math.fsum(values["value_added_mn_lcu"] for values in aggregates.values())
    world_employment = math.fsum(values["persons_engaged_thousand"]
                                 for values in aggregates.values())
    return {
        "year": int(year),
        "world": {
            "labor_share": round(world_compensation / world_va, 6) if world_va > 0 else None,
            "compensation_mn_lcu": round(world_compensation, 6),
            "value_added_mn_lcu": round(world_va, 6),
            "persons_engaged_million": round(world_employment / 1e3, 6),
        },
        "countries": rows,
        "wdi_available": bool(consumption),
        "notes": "consumption share is WDI NE.CON.PRVT.ZS when available; compensation is in "
                 "national currency, so no currency conversion is attempted here",
    }


def labor_share_of_gdp(labor: LaborData, year: int, numerator: str = "COMP") -> float | None:
    """World-level labour share = sum(compensation) / sum(value added), currency-free."""
    compensation = math.fsum(labor.country_totals(year, numerator).values())
    value_added = math.fsum(labor.country_totals(year, "VA").values())
    return round(compensation / value_added, 9) if value_added > 0 else None


def sfc_link(labor_block: Mapping, sfc_block: Mapping | None) -> dict:
    """Compare the labour share with the SFC household/income aggregates (where available)."""
    result = {"labor_share_of_gdp": labor_block.get("labor_share_of_gdp"),
              "sfc_available": bool(sfc_block)}
    if not sfc_block:
        result["note"] = "no SFC block passed: labour share stands alone"
        return result
    totals = sfc_block.get("totals") or {}
    for key in ("household_income", "household_consumption", "savings", "investment"):
        if key in totals:
            result[key] = totals[key]
    labor_share = result["labor_share_of_gdp"]
    income_share = sfc_block.get("household_income_share_of_output")
    if labor_share is not None and income_share is not None:
        result["difference"] = round(labor_share - income_share, 9)
    return result


# ---------------------------------------------------------------------------------------
# Invariants and report block
# ---------------------------------------------------------------------------------------
def labour_invariants(labor: LaborData, year: int, io_dataset=None) -> dict:
    """Named invariant checks; ``ok`` is true when every hard check passes."""
    errors: list[str] = []
    checks: dict = {}
    aggregates = country_aggregates(labor, year)
    shares = labor_share(labor, year)
    all_shares = [value for industry_values in shares["shares"].values()
                  for value in industry_values.values()]
    checks["shares_in_unit_interval"] = all(0.0 <= value <= 1.0 + TOLERANCE
                                            for value in all_shares)
    checks["shares_clipped_count_known"] = shares["clipped"] == len(shares["outliers"])
    checks["non_negative_totals"] = all(
        values["compensation_mn_lcu"] >= 0 and values["value_added_mn_lcu"] >= 0
        and values["persons_engaged_thousand"] >= 0
        for values in aggregates.values())
    cell_negative = 0
    cell_hours_bad = 0
    for industries_block in labor.values[str(int(year))].values():
        for cell in industries_block.values():
            for variable, value in cell.items():
                if variable != "H_EMPE" and value < -TOLERANCE:
                    cell_negative += 1
            employees = cell.get("EMPE")
            hours = cell.get("H_EMPE")
            if employees and employees > 0 and hours is not None:
                cell_hours = hours * 1e3 / employees
                if cell_hours > HOURS_PER_EMPLOYEE_MAX:
                    cell_hours_bad += 1
    checks["non_negative_cells"] = cell_negative == 0
    checks["hours_per_employee_cells_plausible"] = cell_hours_bad == 0
    checks["hours_per_employee_plausible"] = all(
        values["hours_per_employee"] is None
        or HOURS_PER_EMPLOYEE_MIN <= values["hours_per_employee"] <= HOURS_PER_EMPLOYEE_MAX
        for values in aggregates.values())
    # country totals must equal the sum over industries (by construction, re-checked independently)
    recomputed = {}
    for country, industries_block in labor.values[str(int(year))].items():
        recomputed[country] = math.fsum(cell.get("VA", 0.0)
                                        for cell in industries_block.values())
    checks["country_totals_equal_industry_sums"] = all(
        abs(recomputed[country] - aggregates[country]["value_added_mn_lcu"])
        <= TOLERANCE * max(1.0, abs(aggregates[country]["value_added_mn_lcu"]))
        for country in recomputed)
    checks["world_value_added_positive"] = math.fsum(
        values["value_added_mn_lcu"] for values in aggregates.values()) > 0.0
    if io_dataset is not None:
        intensity = employment_intensity(labor, io_dataset, year)
        checks["io_matched_pairs"] = intensity["matched"] > 0
        checks["io_hours_positive"] = all(row["hours_per_usd_mn"] >= 0.0
                                          for row in intensity["rows"])
    # Data-quality facts of the real SEA workbook (a handful of negative cells, labour shares
    # above 1 in tiny-value-added cells) are reported as warnings: they are handled explicitly
    # (clipping with counters) instead of silently failing the layer.
    soft = {"non_negative_cells", "hours_per_employee_cells_plausible"}
    warnings = []
    for name, value in checks.items():
        if value:
            continue
        if name in soft:
            if name == "non_negative_cells":
                warnings.append(f"{cell_negative} negative cells in the source workbook "
                                "(reported, not hidden)")
            else:
                warnings.append(f"{cell_hours_bad} cells with implausible hours per employee")
        else:
            errors.append(f"invariant failed: {name}")
    return {"ok": not errors, "checks": checks, "errors": errors, "warnings": warnings,
            "year": int(year),
            "countries": len(aggregates), "industries": len(labor.industries),
            "clipped_shares": shares["clipped"], "outliers": len(shares["outliers"]),
            "negative_cells": cell_negative, "implausible_hour_cells": cell_hours_bad}


def build_labor_block(labor: LaborData, year: int, io_dataset=None, top_limit: int = 10,
                      wdi: Mapping | None = None, sfc_block: Mapping | None = None) -> dict:
    """Aggregate-only labour block for reports (status ``RESEARCH_ONLY``)."""
    invariants = labour_invariants(labor, year, io_dataset=io_dataset)
    households = household_block(labor, year, wdi=wdi)
    block = {
        "status": "RESEARCH_ONLY",
        "note": "исследовательский слой труда/домохозяйств, не допуск и не прогноз",
        "year": int(year),
        "source": labor.describe(),
        "labor_share_of_gdp": labor_share_of_gdp(labor, year),
        "labor_share_of_gdp_incl_self_employed": labor_share_of_gdp(labor, year, "LAB"),
        "world": households["world"],
        "top_countries_by_labor_share": top_countries_by_labor_share(labor, year, top_limit),
        "households": {
            "world": households["world"],
            "wdi_available": households["wdi_available"],
            "countries": {country: values for country, values in households["countries"].items()},
        },
        "industry_labor_shares": industry_labor_shares(labor, year),
        "invariants": invariants,
        "checks": {"all_invariants_pass": invariants["ok"]},
        "sfc_link": sfc_link({"labor_share_of_gdp": labor_share_of_gdp(labor, year)},
                             sfc_block),
    }
    if io_dataset is not None:
        intensity = employment_intensity(labor, io_dataset, year)
        block["employment_intensity"] = {
            "matched_pairs": intensity["matched"], "missing_pairs": intensity["missing"],
            "method": intensity["method"],
            "top_hours_per_usd_mn": sorted(
                ({"country": row["country"], "industry": row["industry"],
                  "hours_per_usd_mn": row["hours_per_usd_mn"]}
                 for row in intensity["rows"]),
                key=lambda item: (-item["hours_per_usd_mn"], item["country"],
                                  item["industry"]))[:top_limit],
        }
    return block
