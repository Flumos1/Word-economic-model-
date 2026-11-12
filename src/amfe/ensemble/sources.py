"""External forecast sources, provenance classification and SPF horizon rules.

This module is deliberately separate from the ensemble algorithms: it answers *what* each member
is and *when* an external forecast is available, so the weighting/combination code never has to
guess about the provenance of a number.

Source classification (protocol v4 ``licence_policy`` and R3 ``data_manifest``)
-------------------------------------------------------------------------------
``INTERNAL_MODEL``          a model of this project (R3 baselines, ensemble of them).  The code and
                            the numbers are ours; nothing third-party is stored.
``EXTERNAL_FORECAST``       a published third-party forecast (today: the Philadelphia Fed SPF
                            median).  Its values live only in the git-ignored ``data/raw/spf`` and
                            are never committed or published; attribution is mandatory.
``DERIVED_STATISTIC``       a quantity computed by this project from forecasts or actuals (the
                            mean of members, the ensemble weights, empirical SPF error quantiles,
                            concentration diagnostics).  It is not an external forecast even when
                            it is derived from one.
``EXTERNAL_DATA_VINTAGE_SOURCE``  an external *data* archive (RTDSM) that is not a forecast and
                            cannot enter a forecast combination; today it is metadata-only.

SPF availability rules
----------------------
The SPF publishes quarterly medians.  ``medianGrowth.xlsx`` (sheet RGDP) has the published horizon
codes ``drgdp2..drgdp6``: ``drgdp2`` is the current survey quarter and each next code is one quarter
further out (verified on 1990Q1-2026Q3: ``drgdp2`` correlates 0.88 with the survey-quarter growth,
while the other codes do not correlate with any single quarter at that level, consistent with
one-to-four-quarters-ahead forecasts).  A protocol T2 horizon ``h`` therefore maps to
``drgdp(h+2)`` for ``h = 0, 1, 2, 4``; ``drgdp5`` (h=3) is not on the protocol horizon grid.

The survey deadline is the middle month of the survey quarter (fetch_spf anchors ``survey_date`` at
the first day of the middle month; the exact deadline day is not machine-parsed).  This module
treats an SPF forecast as published at an origin only when the origin is a month end of the middle
month and at least ``SPF_MIN_LAG_DAYS`` days after the anchor, which is conservative.

T1 (monthly CPI y/y) has **no** SPF counterpart: the SPF publishes CPI only as quarterly q/q
annualised and Q4/Q4 values, and inventing an intra-quarter path is not allowed (protocol
``origin_policy.spf_alignment``, docs/65).  T3 has no SPF NBER probability either (the SPF
"probability of a decline in real GDP" is a different event and the probability file is not
downloaded).  Both are reported as explicit ``available=False`` reasons instead of being silently
dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"
SPF_METADATA_PATH = ROOT / "protocols" / "spf_metadata.json"

KIND_INTERNAL_MODEL = "INTERNAL_MODEL"
KIND_EXTERNAL_FORECAST = "EXTERNAL_FORECAST"
KIND_DERIVED_STATISTIC = "DERIVED_STATISTIC"
KIND_EXTERNAL_DATA = "EXTERNAL_DATA_VINTAGE_SOURCE"

#: The SPF median GDP growth file: horizon code -> quarter offset from the survey quarter.
SPF_GDP_HORIZON_CODES = {"drgdp2": 0, "drgdp3": 1, "drgdp4": 2, "drgdp5": 3, "drgdp6": 4}
#: Protocol T2 horizon -> published SPF code (drgdp5/h=3 is not on the protocol grid and is omitted).
SPF_GDP_CODE_FOR_HORIZON = {"T2_GDP_GROWTH": {0: "drgdp2", 1: "drgdp3", 2: "drgdp4", 4: "drgdp6"}}
SPF_MIDDLE_MONTHS = {2, 5, 8, 11}
SPF_MIN_LAG_DAYS = 14
SPF_ATTRIBUTION = (
    "Source: Federal Reserve Bank of Philadelphia, Survey of Professional Forecasters, median "
    "forecasts (https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/"
    "survey-of-professional-forecasters); values used under research use terms, attribution required"
)
SPF_VALUES_RULE = (
    "SPF median values are third-party data: they are read from the git-ignored data/raw/spf/ "
    "spf_median.json and are never committed, published, or written to a repository file; only this "
    "project's derived statistics (weights, ensemble values, errors) may be recorded"
)


class EnsembleError(RuntimeError):
    """Base error of the ensemble layer."""


class SourceUnavailable(EnsembleError):
    """An external source is not present locally or has no value for the request."""


@dataclass(frozen=True)
class ExternalForecast:
    """One external forecast value with its provenance and availability verdict."""

    member_id: str
    provider: str
    target: str
    origin: date
    horizon: int
    available: bool
    value: float | None = None
    reason: str | None = None
    code: str | None = None
    survey_quarter: str | None = None
    survey_date: date | None = None
    mapping: str | None = None
    attribution: str = SPF_ATTRIBUTION
    values_location: str = "data/raw/spf/spf_median.json (git-ignored)"

    def to_dict(self) -> dict:
        return {
            "member_id": self.member_id,
            "provider": self.provider,
            "kind": KIND_EXTERNAL_FORECAST,
            "target": self.target,
            "origin": self.origin.isoformat(),
            "horizon": int(self.horizon),
            "available": bool(self.available),
            "value": None if self.value is None else float(self.value),
            "reason": self.reason,
            "code": self.code,
            "survey_quarter": self.survey_quarter,
            "survey_date": None if self.survey_date is None else self.survey_date.isoformat(),
            "mapping": self.mapping,
            "attribution": self.attribution,
            "values_location": self.values_location,
        }


# --------------------------------------------------------------------------------------
# Provenance classification
# --------------------------------------------------------------------------------------
SOURCE_CLASSIFICATION: dict[str, dict] = {
    # R3 baselines and the project's own nowcast: our models, our code, our numbers.
    "RW": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    "AO": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    "AR_BIC": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    "BVAR_MINNESOTA": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    "PROBIT_SPREAD": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    "SAHM_RULE": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    "OWN_NOWCAST_BRIDGE": {"kind": KIND_INTERNAL_MODEL, "provider": "AMFE-R3", "third_party": False},
    # The external forecast itself (third-party values, git-ignored).
    "SPF_GDP_MEDIAN": {
        "kind": KIND_EXTERNAL_FORECAST,
        "provider": "PHILADELPHIA_FED",
        "third_party": True,
        "register": "PHILADELPHIA_FED",
        "attribution": SPF_ATTRIBUTION,
        "values_location": "data/raw/spf/spf_median.json (git-ignored, never committed)",
    },
    # Our statistics computed from the external forecast / from historical errors.
    "SPF_GDP_ERROR_QUANTILES": {
        "kind": KIND_DERIVED_STATISTIC,
        "provider": "AMFE",
        "third_party": False,
        "derived_from": "SPF_GDP_MEDIAN",
    },
    "EQUAL_WEIGHT_MEAN": {
        "kind": KIND_DERIVED_STATISTIC,
        "provider": "AMFE",
        "third_party": False,
        "derived_from": "ensemble members",
    },
    "ENSEMBLE_WEIGHTS": {
        "kind": KIND_DERIVED_STATISTIC,
        "provider": "AMFE",
        "third_party": False,
        "derived_from": "member forecast errors",
    },
    "ENSEMBLE_CONCENTRATION": {
        "kind": KIND_DERIVED_STATISTIC,
        "provider": "AMFE",
        "third_party": False,
        "derived_from": "ensemble weights",
    },
    # External data archive: metadata only, not a forecast and not a combination member.
    "RTDSM": {
        "kind": KIND_EXTERNAL_DATA,
        "provider": "PHILADELPHIA_FED",
        "third_party": True,
        "register": "PHILADELPHIA_FED_RTDSM",
        "status": "METADATA_ONLY",
        "values_location": "not downloaded: values are not used by any model",
    },
}


def classify_member(member_id: str) -> dict:
    """Provenance record of a member or statistic id; unknown ids raise."""
    if member_id not in SOURCE_CLASSIFICATION:
        raise EnsembleError(
            f"unknown source {member_id!r}; add it to SOURCE_CLASSIFICATION before using it"
        )
    return dict(SOURCE_CLASSIFICATION[member_id])


def member_kind(member_id: str) -> str:
    return str(classify_member(member_id)["kind"])


def is_third_party_forecast(member_id: str) -> bool:
    record = classify_member(member_id)
    return record["kind"] == KIND_EXTERNAL_FORECAST and bool(record.get("third_party"))


def validate_member_ids(member_ids) -> list[str]:
    """Classify every id and reject duplicates; returns a list in the given order."""
    ids = list(member_ids)
    if not ids:
        raise EnsembleError("member list is empty")
    if len(set(ids)) != len(ids):
        raise EnsembleError(f"duplicate member ids in {ids}")
    for member_id in ids:
        classify_member(member_id)
    return ids


def rtdsm_status(data_root=None) -> dict:
    """RTDSM availability: metadata only, never a forecast member in this stage."""
    root = Path(data_root) if data_root is not None else Path(
        os.environ.get("AMFE_DATA_ROOT", DEFAULT_DATA_ROOT)
    )
    available = (root / "rtdsm").exists()
    record = dict(SOURCE_CLASSIFICATION["RTDSM"])
    record.update(
        {
            "values_available_locally": bool(available),
            "role": "vintage data channel candidate for GDP/CPI actuals and model inputs; "
                    "not a forecast and not part of the ensemble",
            "note": "RTDSM values were never downloaded (metadata probe only, docs/71); they cannot "
                    "enter the ensemble until the licence reading and the retrieval are done",
        }
    )
    return record


def external_sources_status(data_root=None) -> dict:
    """Availability summary of every external source for reports and tests."""
    return {
        "SPF": {
            "available": spf_available(data_root),
            "kind": KIND_EXTERNAL_FORECAST,
            "values_location": "data/raw/spf/spf_median.json (git-ignored)",
            "attribution": SPF_ATTRIBUTION,
            "publication_rule": SPF_VALUES_RULE,
            "supported_targets": ["T2_GDP_GROWTH"],
            "unsupported_targets": {
                "T1_CPI_YOY": "SPF has no monthly y/y CPI forecast (only q/q annualised and Q4/Q4)",
                "T3_RECESSION": "SPF has no NBER recession probability; the GDP-decline probability "
                                "is a different event and its file is not downloaded",
            },
        },
        "RTDSM": rtdsm_status(data_root),
    }


# --------------------------------------------------------------------------------------
# SPF raw values (third-party values stay in the git-ignored data directory)
# --------------------------------------------------------------------------------------
def spf_values_path(data_root=None) -> Path:
    root = Path(data_root) if data_root is not None else Path(
        os.environ.get("AMFE_DATA_ROOT", DEFAULT_DATA_ROOT)
    )
    return root / "spf" / "spf_median.json"


def spf_available(data_root=None) -> bool:
    return spf_values_path(data_root).exists()


@lru_cache(maxsize=None)
def _spf_cached(path_str: str, mtime_ns: int) -> dict[str, dict[str, dict]]:
    path = Path(path_str)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("series")
    if not isinstance(rows, list) or not rows:
        raise SourceUnavailable(f"SPF values file has no 'series' list: {path}")
    table: dict[str, dict[str, dict]] = {}
    for row in rows:
        try:
            code = str(row["code"])
            quarter = str(row["survey_quarter"])
            value = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        table.setdefault(code, {})[quarter] = {
            "value": value,
            "survey_date": str(row.get("survey_date", "")),
            "variable": row.get("variable"),
            "horizon": row.get("horizon"),
        }
    if not table:
        raise SourceUnavailable(f"SPF values file has no usable rows: {path}")
    return table


def load_spf_medians(data_root=None) -> dict[str, dict[str, dict]]:
    """{code: {survey_quarter: {value, survey_date, variable, horizon}}} from the local file."""
    path = spf_values_path(data_root)
    if not path.exists():
        raise SourceUnavailable(
            f"SPF values are not present locally: {path} (git-ignored third-party data; run "
            "tools/fetch_spf.py)"
        )
    return _spf_cached(str(path), path.stat().st_mtime_ns)


def survey_middle_month(quarter: int) -> int:
    """Month index (year*12 + month-1) of the middle month of a survey quarter."""
    qtr = quarter % 4  # 0..3
    return (quarter // 4) * 12 + 3 * qtr + 1


def survey_quarter_label(quarter: int) -> str:
    return f"{quarter // 4}Q{quarter % 4 + 1}"


def survey_quarter_for_origin(origin) -> int | None:
    """Survey quarter whose deadline falls in the origin month, or ``None``.

    The deadline is the middle month of the survey quarter; the origin must be that month end.
    """
    if hasattr(origin, "year"):
        d = origin
    else:
        from amfe.forecast.data import as_date

        d = as_date(origin)
    if d.month not in SPF_MIDDLE_MONTHS:
        return None
    quarter = d.year * 4 + (d.month - 1) // 3
    return quarter


def spf_forecast(
    target: str,
    origin,
    horizon: int,
    *,
    data_root=None,
    member_id: str = "SPF_GDP_MEDIAN",
) -> ExternalForecast:
    """SPF median point forecast for one target / horizon / origin, with an explicit verdict."""
    from amfe.forecast.data import as_date, month_end_of

    origin_d = month_end_of(as_date(origin))
    horizon_i = int(horizon)
    reasons = {
        "T1_CPI_YOY": "NO_MONTHLY_YOY_CPI_FORECAST",
        "T3_RECESSION": "NO_NBER_RECESSION_PROBABILITY_AND_EVENT_MISMATCH",
    }
    if target in reasons:
        return ExternalForecast(
            member_id=member_id, provider="PHILADELPHIA_FED", target=target,
            origin=origin_d, horizon=horizon_i, available=False, reason=reasons[target],
        )
    if target != "T2_GDP_GROWTH":
        return ExternalForecast(
            member_id=member_id, provider="PHILADELPHIA_FED", target=target,
            origin=origin_d, horizon=horizon_i, available=False,
            reason=f"UNSUPPORTED_TARGET_{target}",
        )
    code = SPF_GDP_CODE_FOR_HORIZON["T2_GDP_GROWTH"].get(horizon_i)
    if code is None:
        return ExternalForecast(
            member_id=member_id, provider="PHILADELPHIA_FED", target=target,
            origin=origin_d, horizon=horizon_i, available=False,
            reason=f"NO_SPF_HORIZON_CODE_FOR_H{horizon_i}",
            mapping="drgdp(h+2); h=3 has no protocol horizon entry",
        )
    survey_quarter = survey_quarter_for_origin(origin_d)
    if survey_quarter is None:
        return ExternalForecast(
            member_id=member_id, provider="PHILADELPHIA_FED", target=target,
            origin=origin_d, horizon=horizon_i, available=False,
            reason="ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH",
            code=code, mapping="drgdp(h+2)",
        )
    try:
        table = load_spf_medians(data_root)
    except SourceUnavailable:
        return ExternalForecast(
            member_id=member_id, provider="PHILADELPHIA_FED", target=target,
            origin=origin_d, horizon=horizon_i, available=False,
            reason="SPF_VALUES_NOT_DOWNLOADED", code=code,
            survey_quarter=survey_quarter_label(survey_quarter),
            mapping="drgdp(h+2)",
        )
    quarter_label = survey_quarter_label(survey_quarter)
    entry = table.get(code, {}).get(quarter_label)
    if entry is None:
        return ExternalForecast(
            member_id=member_id, provider="PHILADELPHIA_FED", target=target,
            origin=origin_d, horizon=horizon_i, available=False,
            reason="SURVEY_QUARTER_NOT_IN_SPF_FILE", code=code,
            survey_quarter=quarter_label, mapping="drgdp(h+2)",
        )
    survey_date = None
    try:
        survey_date = date.fromisoformat(entry["survey_date"][:10])
    except (KeyError, TypeError, ValueError):
        survey_date = None
    if survey_date is not None:
        lag_days = (origin_d - survey_date).days
        if lag_days < SPF_MIN_LAG_DAYS:
            return ExternalForecast(
                member_id=member_id, provider="PHILADELPHIA_FED", target=target,
                origin=origin_d, horizon=horizon_i, available=False,
                reason="SPF_DEADLINE_NOT_REACHED_AT_ORIGIN", code=code,
                survey_quarter=quarter_label, survey_date=survey_date,
                mapping="drgdp(h+2)",
            )
    return ExternalForecast(
        member_id=member_id,
        provider="PHILADELPHIA_FED",
        target=target,
        origin=origin_d,
        horizon=horizon_i,
        available=True,
        value=float(entry["value"]),
        code=code,
        survey_quarter=quarter_label,
        survey_date=survey_date,
        mapping="drgdp2=current survey quarter, drgdpN=survey quarter+(N-2); T2 h -> drgdp(h+2)",
        attribution=SPF_ATTRIBUTION,
    )
