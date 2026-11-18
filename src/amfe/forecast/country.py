"""Country-level nowcast/forecast layer for the euro area, China and the UK.

Task-60 / UC-06 (country growth), UC-07 (country risk input) and UC-08 (FX context).
This module is deliberately **RESEARCH_ONLY**: it does not assign an admission stage,
does not claim C1/C2/C4/C6, and does not replace the US protocol targets T1/T2/T3.

What is here
------------
* a small data adapter with three providers and explicit provenance:
  - ``LOCAL_PIT_CSV`` - quarterly/annual CSV with ``period,value,release_date,vintage``
    dropped into ``data/external/country/<CC>_<TARGET>_<FREQ>.csv`` (git-ignored);
  - ``WORLD_BANK_WDI`` - the existing annual WDI cache in ``data/mrio/wb`` (CC BY 4.0);
  - ``SYNTHETIC_FIXTURE`` - a deterministic fixture used only when no real series is
    available; every forecast and every OOS number built on it is marked synthetic.
* point-in-time (PIT) slicing: strict release-date filtering for real/synthetic vintage
  series, and an explicitly labelled final-vintage mode for WDI (no release dates).
* four simple models: ``RW`` (benchmark), ``AR_BIC`` (univariate AR, BIC order),
  ``PANEL_AR1`` (pooled AR(1) with country means) and ``BRIDGE`` (single-equation
  bridge with an auxiliary AR(1) for the other country target).
* walk-forward OOS by horizon 1..4 with RMSE/MAE/bias and relative RMSE versus RW.
* a compact report builder that contains aggregates only - no historical values.

Design rules
------------
* Only the standard library and numpy are used; no network calls, no pandas.
* A real series is never replaced by a synthetic one silently: the report carries
  ``source_class``, ``pit_status``, ``real`` and ``reason`` for every country/target.
* Missing data is ``NOT_AVAILABLE``; it is never imputed for a *real* country claim.
* Every forecast target period is strictly after the last period used for estimation.
* PIT violations raise :class:`PITViolationError`; the CLI is fail-closed.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EXTERNAL_ROOT = ROOT / "data" / "external"
DEFAULT_WB_ROOT = ROOT / "data" / "mrio" / "wb"

COUNTRY_IDS: tuple[str, ...] = ("EA", "CN", "GB")
COUNTRY_NAMES = {"EA": "Euro area", "CN": "China", "GB": "United Kingdom"}
COUNTRY_ALIASES = {
    "EA": "EA",
    "EUR": "EA",
    "EURO": "EA",
    "EUROAREA": "EA",
    "EUROZONE": "EA",
    "CN": "CN",
    "CHN": "CN",
    "CHINA": "CN",
    "GB": "GB",
    "GBR": "GB",
    "UK": "GB",
    "UNITEDKINGDOM": "GB",
}

TARGET_IDS: tuple[str, ...] = ("GDP", "CPI")
TARGET_NAMES = {"GDP": "real GDP growth (% y/y / % annual)", "CPI": "CPI inflation (% y/y / % annual)"}
FREQ_IDS: tuple[str, ...] = ("Q", "A")
FREQ_LABELS = {"Q": "quarterly", "A": "annual"}
HORIZONS: tuple[int, ...] = (1, 2, 3, 4)
MODEL_IDS: tuple[str, ...] = ("RW", "AR_BIC", "PANEL_AR1", "BRIDGE")
MODEL_NAMES = {
    "RW": "random walk (last published value)",
    "AR_BIC": "univariate AR(p), BIC order <= 4",
    "PANEL_AR1": "pooled AR(1) with country means",
    "BRIDGE": "bridge on the other target with an auxiliary AR(1)",
}

#: minimum training observations for a model at an origin
MIN_TRAIN = {"Q": 20, "A": 12}
#: minimum number of OOS origins for a metric to be reported as OK
MIN_OOS = {"Q": 20, "A": 8}
#: minimum panel members (including the target country)
MIN_PANEL_MEMBERS = 2
MIN_PANEL_OBS = 8
#: minimum aligned observations for the bridge
MIN_BRIDGE_OBS = 10

DATA_CLASS_REAL = "REAL"
DATA_CLASS_SYNTHETIC = "SYNTHETIC_FIXTURE"
DATA_CLASS_NOT_AVAILABLE = "NOT_AVAILABLE"

PIT_REAL = "REAL_PIT"
PIT_SYNTHETIC = "SYNTHETIC_PIT"
PIT_FINAL_VINTAGE = "FINAL_VINTAGE_NO_PIT"
PIT_NOT_AVAILABLE = "NOT_AVAILABLE"

STATUS_REAL = "REAL"
STATUS_SYNTHETIC = "SYNTHETIC_FIXTURE"
STATUS_MIXED = "MIXED"
STATUS_NOT_AVAILABLE = "NOT_AVAILABLE"
STATUS_RESEARCH_ONLY = "RESEARCH_ONLY"

Z80 = 1.2815515655446004
DIGEST_EXCLUDES = ("generated_at", "content_sha256")

WB_INDICATORS = {"GDP": "NY.GDP.MKTP.KD.ZG", "CPI": "FP.CPI.TOTL.ZG"}
WB_FILES = {"GDP": "wb_NY.GDP.MKTP.KD.ZG.json", "CPI": "wb_FP.CPI.TOTL.ZG.json"}
WB_COUNTRY = {"CN": "CHN", "GB": "GBR"}

SYNTHETIC_START_Q = 2000 * 4 + 0
SYNTHETIC_END_Q = 2026 * 4 + 1  # 2026Q2
SYNTHETIC_START_A = 1995
SYNTHETIC_END_A = 2024
SYNTHETIC_RELEASE_LAG_DAYS = {"GDP": {"Q": 45, "A": 90}, "CPI": {"Q": 30, "A": 60}}

FORBIDDEN_REPORT_KEYS = frozenset(
    {"observations", "values", "series_values", "raw_values", "history", "raw", "vintages"}
)


class CountryForecastError(RuntimeError):
    """Base class for country-layer errors."""


class DataNotAvailable(CountryForecastError):
    """No real (or allowed) series exists for the requested country/target/frequency."""


class PITViolationError(CountryForecastError):
    """A point-in-time contract failed (future release, missing release date, etc.)."""


class InsufficientHistory(CountryForecastError):
    """Not enough observations at the origin to estimate the model."""


class ReportIntegrityError(CountryForecastError):
    """The generated report would violate the aggregates-only / fail-closed rules."""


# --------------------------------------------------------------------------------------
# Calendar helpers
# --------------------------------------------------------------------------------------
def as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:  # pragma: no cover - defensive
        raise CountryForecastError(f"cannot parse date {value!r}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def period_end(index: int, freq: str) -> date:
    """Last calendar day of a quarter/year period ordinal."""
    _check_freq(freq)
    index = int(index)
    if freq == "Q":
        year, quarter = divmod(index, 4)
        month = quarter * 3 + 3
        return date(year, month, calendar.monthrange(year, month)[1])
    return date(index, 12, 31)


def period_start(index: int, freq: str) -> date:
    _check_freq(freq)
    index = int(index)
    if freq == "Q":
        year, quarter = divmod(index, 4)
        return date(year, quarter * 3 + 1, 1)
    return date(index, 1, 1)


def period_label(index: int, freq: str) -> str:
    _check_freq(freq)
    index = int(index)
    if freq == "Q":
        year, quarter = divmod(index, 4)
        return f"{year}Q{quarter + 1}"
    return f"{index}"


def parse_period(text: str, freq: str) -> int:
    _check_freq(freq)
    raw = str(text).strip()
    if freq == "Q":
        match = re.fullmatch(r"(\d{4})[\s\-_/]?[Qq]([1-4])", raw)
        if not match:
            raise CountryForecastError(f"cannot parse quarterly period {text!r}; expected YYYYQn")
        return int(match.group(1)) * 4 + (int(match.group(2)) - 1)
    if not re.fullmatch(r"\d{4}", raw):
        raise CountryForecastError(f"cannot parse annual period {text!r}; expected YYYY")
    return int(raw)


def next_period(index: int, horizon: int) -> int:
    if int(horizon) < 1:
        raise ValueError("horizon must be a positive integer")
    return int(index) + int(horizon)


def _check_freq(freq: str) -> None:
    if freq not in FREQ_IDS:
        raise ValueError(f"unknown frequency {freq!r}; expected one of {FREQ_IDS}")


def normalize_country(value: str) -> str:
    key = re.sub(r"[^A-Z]", "", str(value).strip().upper())
    if key not in COUNTRY_ALIASES:
        raise ValueError(f"unknown country {value!r}; expected one of {COUNTRY_IDS}")
    return COUNTRY_ALIASES[key]


def normalize_target(value: str) -> str:
    key = str(value).strip().upper()
    if key not in TARGET_IDS:
        raise ValueError(f"unknown target {value!r}; expected one of {TARGET_IDS}")
    return key


def normalize_frequency(value: str) -> str:
    key = str(value).strip().upper()
    aliases = {"Q": "Q", "QUARTERLY": "Q", "QUARTER": "Q", "A": "A", "ANNUAL": "A", "Y": "A", "YEARLY": "A"}
    if key not in aliases:
        raise ValueError(f"unknown frequency {value!r}; expected quarterly or annual")
    return aliases[key]


# --------------------------------------------------------------------------------------
# Series container
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Observation:
    """One country observation: period ordinal, value, optional release/vintage."""

    period: int
    value: float
    release_date: date | None = None
    vintage: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", int(self.period))
        value = float(self.value)
        if not math.isfinite(value):
            raise CountryForecastError("observation value must be finite")
        object.__setattr__(self, "value", value)
        if self.release_date is not None and not isinstance(self.release_date, date):
            object.__setattr__(self, "release_date", as_date(self.release_date))
        if self.vintage is not None:
            object.__setattr__(self, "vintage", str(self.vintage))


@dataclass(frozen=True)
class CountrySeries:
    """A country/target series with explicit provenance and PIT mode."""

    country: str
    target: str
    freq: str
    observations: tuple[Observation, ...]
    source: str
    source_id: str
    source_class: str
    pit_status: str
    licence: str
    retrieved_at: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        country = normalize_country(self.country)
        target = normalize_target(self.target)
        _check_freq(self.freq)
        if self.source_class not in (DATA_CLASS_REAL, DATA_CLASS_SYNTHETIC):
            raise ValueError(f"unknown source_class {self.source_class!r}")
        if self.pit_status not in (PIT_REAL, PIT_SYNTHETIC, PIT_FINAL_VINTAGE):
            raise ValueError(f"unknown pit_status {self.pit_status!r}")
        obs = tuple(sorted((o if isinstance(o, Observation) else Observation(**o) for o in self.observations),
                           key=lambda o: o.period))
        periods = [o.period for o in obs]
        if len(set(periods)) != len(periods):
            raise CountryForecastError("duplicate observation periods")
        for item in obs:
            if item.release_date is not None and item.release_date < period_end(item.period, self.freq):
                raise PITViolationError(
                    f"{country}/{target}/{self.freq}: release {item.release_date} precedes period end "
                    f"{period_end(item.period, self.freq)} for {period_label(item.period, self.freq)}"
                )
        if self.pit_status in (PIT_REAL, PIT_SYNTHETIC) and any(o.release_date is None for o in obs):
            raise PITViolationError(
                f"{country}/{target}: pit_status={self.pit_status} requires a release_date on every observation"
            )
        object.__setattr__(self, "country", country)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "observations", obs)
        object.__setattr__(self, "notes", tuple(str(n) for n in self.notes))

    # -- accessors ---------------------------------------------------------------------
    @property
    def n_obs(self) -> int:
        return len(self.observations)

    @property
    def is_synthetic(self) -> bool:
        return self.source_class == DATA_CLASS_SYNTHETIC

    @property
    def first_period(self) -> int | None:
        return self.observations[0].period if self.observations else None

    @property
    def last_period(self) -> int | None:
        return self.observations[-1].period if self.observations else None

    @property
    def last_value(self) -> float | None:
        return self.observations[-1].value if self.observations else None

    @property
    def last_release_date(self) -> date | None:
        if not self.observations:
            return None
        return max((o.release_date or period_end(o.period, self.freq)) for o in self.observations)

    @property
    def max_release_date(self) -> date | None:
        return self.last_release_date

    def period_label(self, period: int | None = None) -> str:
        period = self.last_period if period is None else int(period)
        return "none" if period is None else period_label(period, self.freq)

    # -- PIT ----------------------------------------------------------------------------
    def as_of(self, origin) -> "CountrySeries":
        """Slice the series to what was published on or before ``origin``."""
        origin_d = as_date(origin)
        if self.pit_status == PIT_FINAL_VINTAGE:
            keep = [o for o in self.observations if period_end(o.period, self.freq) <= origin_d]
        elif self.pit_status in (PIT_REAL, PIT_SYNTHETIC):
            if any(o.release_date is None for o in self.observations):
                raise PITViolationError(f"{self.country}/{self.target}: missing release_date")
            keep = [o for o in self.observations if o.release_date <= origin_d]
        else:  # pragma: no cover - constructor guards
            raise DataNotAvailable(f"{self.country}/{self.target}: pit_status={self.pit_status}")
        return replace(self, observations=tuple(keep))

    def assert_pit_clean(self, origin) -> None:
        """Raise if any observation in this already-sliced series is after ``origin``."""
        origin_d = as_date(origin)
        if self.pit_status == PIT_FINAL_VINTAGE:
            bad = [o for o in self.observations if period_end(o.period, self.freq) > origin_d]
        else:
            bad = [o for o in self.observations if o.release_date is None or o.release_date > origin_d]
        if bad:
            example = bad[0]
            raise PITViolationError(
                f"{self.country}/{self.target}/{self.freq}: observation {period_label(example.period, self.freq)} "
                f"(release {example.release_date}) is after origin {origin_d.isoformat()}"
            )

    def values(self) -> np.ndarray:
        return np.asarray([o.value for o in self.observations], dtype=float)

    def periods(self) -> np.ndarray:
        return np.asarray([o.period for o in self.observations], dtype=np.int64)

    def describe(self) -> dict:
        return {
            "country": self.country,
            "target": self.target,
            "frequency": self.freq,
            "source_id": self.source_id,
            "source": self.source,
            "source_class": self.source_class,
            "pit_status": self.pit_status,
            "licence": self.licence,
            "retrieved_at": self.retrieved_at,
            "n_observations": self.n_obs,
            "first_period": self.period_label(self.first_period),
            "last_period": self.period_label(self.last_period),
            "last_release": self.last_release_date.isoformat() if self.last_release_date else None,
            "real": not self.is_synthetic,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceProbe:
    provider: str
    country: str
    target: str
    freq: str
    status: str
    detail: str
    observations: int = 0
    real: bool = False

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "country": self.country,
            "target": self.target,
            "frequency": self.freq,
            "status": self.status,
            "detail": self.detail,
            "n_observations": int(self.observations),
            "real": bool(self.real),
        }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _parse_float(text, *, field: str) -> float:
    raw = str(text).strip()
    if raw in ("", ".", "NA", "N/A", "nan", "NaN", "null"):
        raise CountryForecastError(f"missing {field}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise CountryForecastError(f"non-numeric {field}: {raw!r}") from exc
    if not math.isfinite(value):
        raise CountryForecastError(f"non-finite {field}")
    return value


def local_series_path(external_root, country: str, target: str, freq: str) -> Path:
    return Path(external_root) / "country" / f"{country}_{target}_{freq}.csv"


def load_local_pit_series(
    external_root,
    country: str,
    target: str,
    freq: str,
) -> tuple[CountrySeries | None, SourceProbe]:
    """Load an optional git-ignored PIT CSV: ``period,value,release_date,vintage``.

    A missing file is a normal ``NOT_AVAILABLE`` probe, not an error.  If the file
    exists but is malformed, the error is raised (fail-closed) rather than silently
    falling back to synthetic data.
    """
    country = normalize_country(country)
    target = normalize_target(target)
    _check_freq(freq)
    path = local_series_path(external_root, country, target, freq)
    if not path.exists():
        return None, SourceProbe(
            "LOCAL_PIT_CSV", country, target, freq, "NOT_AVAILABLE",
            f"file not found: {_display_path(path)}", 0, False,
        )
    rows: list[Observation] = []
    has_release = False
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CountryForecastError(f"{_display_path(path)}: empty CSV")
        fields = {name.strip().lower() for name in reader.fieldnames if name}
        if not {"period", "value"}.issubset(fields):
            raise CountryForecastError(f"{_display_path(path)}: required columns period,value")
        for line_no, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            period = parse_period(row.get("period", ""), freq)
            value = _parse_float(row.get("value", ""), field=f"value (line {line_no})")
            release_text = (row.get("release_date") or "").strip()
            release = as_date(release_text) if release_text else None
            vintage = (row.get("vintage") or "").strip() or None
            if release is not None:
                has_release = True
            rows.append(Observation(period=period, value=value, release_date=release, vintage=vintage))
    if not rows:
        return None, SourceProbe(
            "LOCAL_PIT_CSV", country, target, freq, "EMPTY",
            f"no observations in {_display_path(path)}", 0, False,
        )
    pit_status = PIT_REAL if has_release and all(o.release_date is not None for o in rows) else PIT_FINAL_VINTAGE
    series = CountrySeries(
        country=country,
        target=target,
        freq=freq,
        observations=tuple(rows),
        source=f"{_display_path(path)} sha256={_sha256_file(path)}",
        source_id="LOCAL_PIT_CSV",
        source_class=DATA_CLASS_REAL,
        pit_status=pit_status,
        licence="user-provided local file; licence is the responsibility of the provider",
        retrieved_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat(),
        notes=(
            "local PIT CSV; release_date present" if pit_status == PIT_REAL
            else "local CSV without complete release dates -> FINAL_VINTAGE mode (revision bias)",
        ),
    )
    return series, SourceProbe(
        "LOCAL_PIT_CSV", country, target, freq, "AVAILABLE",
        f"loaded {series.n_obs} observations from {_display_path(path)}", series.n_obs, True,
    )


def load_world_bank_series(
    wb_root,
    country: str,
    target: str,
    freq: str,
) -> tuple[CountrySeries | None, SourceProbe]:
    """Load the existing annual WDI cache (World Bank, CC BY 4.0).

    WDI is annual and the cache stores only final values: no vintages and no release
    dates.  The series is therefore marked ``FINAL_VINTAGE_NO_PIT``; quarterly
    requests get an explicit ``FREQUENCY_MISMATCH`` probe.  There is no euro-area
    aggregate in the cache, and the module does not aggregate member states.
    """
    country = normalize_country(country)
    target = normalize_target(target)
    _check_freq(freq)
    if freq != "A":
        return None, SourceProbe(
            "WORLD_BANK_WDI", country, target, freq, "FREQUENCY_MISMATCH",
            "WDI cache is annual; no quarterly vintage/release-date source is present locally",
            0, False,
        )
    if country == "EA":
        return None, SourceProbe(
            "WORLD_BANK_WDI", country, target, freq, "NO_COUNTRY_AGGREGATE",
            "WDI has no euro-area aggregate in this cache; member-state aggregation is not attempted",
            0, False,
        )
    iso3 = WB_COUNTRY.get(country)
    if iso3 is None:
        return None, SourceProbe("WORLD_BANK_WDI", country, target, freq, "NO_COUNTRY_AGGREGATE",
                                 "no WDI mapping for this country", 0, False)
    path = Path(wb_root) / WB_FILES[target]
    if not path.exists():
        return None, SourceProbe("WORLD_BANK_WDI", country, target, freq, "NOT_AVAILABLE",
                                 f"file not found: {_display_path(path)}", 0, False)
    document = json.loads(path.read_text(encoding="utf-8"))
    indicator = document.get("indicator")
    if indicator != WB_INDICATORS[target]:
        raise CountryForecastError(
            f"{_display_path(path)}: indicator {indicator!r} does not match {WB_INDICATORS[target]!r}"
        )
    payload = document.get("payload")
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else payload
    if not isinstance(rows, list):
        raise CountryForecastError(f"{_display_path(path)}: unexpected WDI payload layout")
    observations: list[Observation] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("countryiso3code") != iso3:
            continue
        value = row.get("value")
        if value is None:
            continue
        try:
            period = int(str(row.get("date", "")).strip())
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        observations.append(Observation(period=period, value=number, release_date=None, vintage=None))
    if not observations:
        return None, SourceProbe("WORLD_BANK_WDI", country, target, freq, "EMPTY",
                                 f"no non-missing WDI rows for {iso3} in {_display_path(path)}", 0, False)
    series = CountrySeries(
        country=country,
        target=target,
        freq="A",
        observations=tuple(observations),
        source=f"{_display_path(path)} sha256={_sha256_file(path)}",
        source_id="WORLD_BANK",
        source_class=DATA_CLASS_REAL,
        pit_status=PIT_FINAL_VINTAGE,
        licence="CC BY 4.0 (World Bank WDI); attribution required",
        retrieved_at=str(document.get("retrieved_at") or "") or None,
        notes=(
            "WDI final vintage; revisions are not stored and release dates are unavailable",
            "annual only; not a quarterly PIT source",
            "see protocols/source_registry.json (WORLD_BANK, VERIFIED) and docs/128",
        ),
    )
    return series, SourceProbe(
        "WORLD_BANK_WDI", country, target, freq, "AVAILABLE",
        f"loaded {series.n_obs} annual observations from {_display_path(path)}", series.n_obs, True,
    )


def _synthetic_seed(country: str, target: str, freq: str) -> int:
    digest = hashlib.sha256(f"AMFE-COUNTRY-SYNTHETIC|{country}|{target}|{freq}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _synthetic_path(rng, n: int, mean: float, rho: float, sd: float) -> np.ndarray:
    shocks = rng.normal(0.0, sd, size=n)
    values = np.empty(n, dtype=float)
    values[0] = mean + shocks[0]
    for index in range(1, n):
        values[index] = mean + rho * (values[index - 1] - mean) + shocks[index]
    return values


def synthetic_series(country: str, target: str, freq: str) -> CountrySeries:
    """Deterministic fixture: plausible-looking but explicitly fake country data.

    The fixture exists to exercise PIT slicing, the models and walk-forward OOS on a
    machine that has only US series in ``data/raw``.  It is **not** a substitute for
    real EA/CN/GB data and every report built on it says so.
    """
    country = normalize_country(country)
    target = normalize_target(target)
    _check_freq(freq)
    rng = np.random.default_rng(_synthetic_seed(country, target, freq))
    if freq == "Q":
        periods = list(range(SYNTHETIC_START_Q, SYNTHETIC_END_Q + 1))
    else:
        periods = list(range(SYNTHETIC_START_A, SYNTHETIC_END_A + 1))
    n = len(periods)
    means = {
        "EA": {"GDP": 1.5, "CPI": 1.9},
        "CN": {"GDP": 6.0, "CPI": 2.1},
        "GB": {"GDP": 1.8, "CPI": 2.2},
    }
    mean = means[country][target]
    if target == "GDP":
        values = _synthetic_path(rng, n, mean=mean, rho=0.55, sd=0.9)
        # deterministic recession-like dips, every 28 quarters/years
        for start in range(10, n, 28):
            for offset in range(3):
                if start + offset < n:
                    values[start + offset] -= 1.2 * (3 - offset) / 3.0
        values = np.clip(values, -8.0, 12.0)
    else:
        values = _synthetic_path(rng, n, mean=mean, rho=0.78, sd=0.35)
        # an inflation surge around 2022 to make the OOS exercise non-trivial
        surge_centre = 0
        for position, period in enumerate(periods):
            year = period // 4 if freq == "Q" else period
            if year == 2022:
                surge_centre = position
                break
        for position in range(n):
            values[position] += 2.8 * math.exp(-((position - surge_centre) / 3.5) ** 2)
        values = np.clip(values, -1.0, 12.0)
    lag = SYNTHETIC_RELEASE_LAG_DAYS[target][freq]
    observations = []
    for period, value in zip(periods, values):
        release = period_end(period, freq) + timedelta(days=lag)
        observations.append(Observation(period=int(period), value=float(value), release_date=release,
                                        vintage=f"SYNTH-{release.isoformat()}"))
    return CountrySeries(
        country=country,
        target=target,
        freq=freq,
        observations=tuple(observations),
        source=f"synthetic:{country}:{target}:{freq}:seed={_synthetic_seed(country, target, freq)}",
        source_id="SYNTHETIC_FIXTURE",
        source_class=DATA_CLASS_SYNTHETIC,
        pit_status=PIT_SYNTHETIC,
        licence="synthetic fixture; no third-party data; not for real-country claims",
        retrieved_at=None,
        notes=(
            "deterministic synthetic fixture, generated locally",
            "NOT_AVAILABLE for real EA/CN/GB inference; pipeline verification only",
            "synthetic release dates are period end + fixed lag",
        ),
    )


def load_series(
    country: str,
    target: str,
    freq: str,
    *,
    external_root=None,
    wb_root=None,
    allow_synthetic: bool = True,
) -> tuple[CountrySeries | None, list[SourceProbe]]:
    """Resolve one country/target/frequency series and return all probes.

    Order: local PIT CSV -> World Bank WDI (annual only) -> synthetic fixture (if
    allowed).  With ``allow_synthetic=False`` a missing real series raises
    :class:`DataNotAvailable` after the probes are recorded.
    """
    country = normalize_country(country)
    target = normalize_target(target)
    _check_freq(freq)
    external_root = Path(external_root) if external_root is not None else DEFAULT_EXTERNAL_ROOT
    wb_root = Path(wb_root) if wb_root is not None else DEFAULT_WB_ROOT
    probes: list[SourceProbe] = []
    local, local_probe = load_local_pit_series(external_root, country, target, freq)
    probes.append(local_probe)
    if local is not None:
        return local, probes
    bank, bank_probe = load_world_bank_series(wb_root, country, target, freq)
    probes.append(bank_probe)
    if bank is not None:
        return bank, probes
    if allow_synthetic:
        synthetic = synthetic_series(country, target, freq)
        probes.append(SourceProbe(
            "SYNTHETIC_FIXTURE", country, target, freq, "AVAILABLE",
            "deterministic synthetic fixture; real country data NOT_AVAILABLE", synthetic.n_obs, False,
        ))
        return synthetic, probes
    reasons = "; ".join(f"{probe.provider}: {probe.status} ({probe.detail})" for probe in probes)
    raise DataNotAvailable(
        f"{country}/{target}/{freq}: no real series and synthetic fallback is disabled. {reasons}"
    )


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ForecastResult:
    country: str
    target: str
    freq: str
    model: str
    origin_date: date
    origin_period: int
    target_period: int
    horizon: int
    point: float
    lo80: float
    hi80: float
    n_obs: int
    order: int | None
    pit_status: str
    data_status: str
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "country": self.country,
            "target": self.target,
            "frequency": self.freq,
            "model": self.model,
            "origin_date": self.origin_date.isoformat(),
            "origin_period": period_label(self.origin_period, self.freq),
            "origin_period_index": int(self.origin_period),
            "target_period": period_label(self.target_period, self.freq),
            "target_period_index": int(self.target_period),
            "horizon": int(self.horizon),
            "point": float(self.point),
            "interval80": [float(self.lo80), float(self.hi80)],
            "n_obs": int(self.n_obs),
            "order": None if self.order is None else int(self.order),
            "pit_status": self.pit_status,
            "data_status": self.data_status,
            "notes": list(self.notes),
        }


def _min_train(freq: str) -> int:
    return MIN_TRAIN[freq]


def _fit_ar(values: np.ndarray, max_order: int = 4) -> tuple[int, np.ndarray, float, np.ndarray]:
    """AR(p) with intercept; BIC order selection; stability projection."""
    values = np.asarray(values, dtype=float).ravel()
    n = values.size
    if n < 8:
        raise InsufficientHistory(f"AR needs at least 8 observations, got {n}")
    best: tuple[float, int, np.ndarray, np.ndarray] | None = None
    max_p = min(int(max_order), max(1, (n - 3) // 3))
    for order in range(1, max_p + 1):
        rows = n - order
        columns = [np.ones(rows, dtype=float)]
        for lag in range(1, order + 1):
            columns.append(values[order - lag:n - lag])
        design = np.column_stack(columns)
        target = values[order:]
        beta, *_ = np.linalg.lstsq(design, target, rcond=None)
        residual = target - design @ beta
        rss = float(np.dot(residual, residual))
        if rss <= 1e-12:
            rss = 1e-12
        bic = rows * math.log(rss / rows) + (order + 1) * math.log(rows)
        if best is None or bic < best[0] - 1e-12:
            best = (bic, order, beta, residual)
    if best is None:  # pragma: no cover - defensive
        raise InsufficientHistory("AR order selection failed")
    _bic, order, beta, residual = best
    coefs = _stabilize_ar(beta[1:])
    beta = np.concatenate(([float(beta[0])], coefs))
    sd = float(np.std(residual, ddof=1)) if residual.size > 1 else 0.0
    if not math.isfinite(sd) or sd <= 0:
        sd = max(abs(float(np.mean(values))) * 0.01, 1e-6)
    return order, beta, sd, residual


def _stabilize_ar(coefs: np.ndarray, limit: float = 0.99) -> np.ndarray:
    coefs = np.asarray(coefs, dtype=float)
    if coefs.size == 0:
        return coefs
    roots = np.roots(np.concatenate(([1.0], -coefs)))
    if roots.size == 0:
        return coefs
    largest = float(np.max(np.abs(roots)))
    if largest > limit > 0:
        coefs = coefs * (limit / largest)
    return coefs


def _ar_path(beta: np.ndarray, order: int, history: np.ndarray, horizon: int) -> np.ndarray:
    values = [float(v) for v in history]
    for _ in range(int(horizon)):
        row = np.asarray([1.0] + values[-order:][::-1], dtype=float)
        values.append(float(row @ beta))
    return np.asarray(values[-int(horizon):], dtype=float)


def _fit_panel(panel: Mapping[str, np.ndarray]) -> tuple[float, float, dict[str, float]]:
    """Pooled AR(1) with country means; returns rho, residual sd and country means."""
    xs: list[np.ndarray] = []
    zs: list[np.ndarray] = []
    means: dict[str, float] = {}
    for name, values in panel.items():
        values = np.asarray(values, dtype=float).ravel()
        if values.size < MIN_PANEL_OBS:
            continue
        mean = float(np.mean(values))
        means[name] = mean
        xs.append(values[:-1] - mean)
        zs.append(values[1:] - mean)
    if len(xs) < MIN_PANEL_MEMBERS:
        raise InsufficientHistory(
            f"panel needs at least {MIN_PANEL_MEMBERS} countries with >= {MIN_PANEL_OBS} observations"
        )
    x = np.concatenate(xs)
    z = np.concatenate(zs)
    denominator = float(np.dot(x, x))
    rho = 0.0 if denominator <= 0 else float(np.dot(x, z) / denominator)
    rho = float(np.clip(rho, -0.99, 0.99))
    residual = z - rho * x
    sd = float(np.std(residual, ddof=1)) if residual.size > 1 else 0.0
    if not math.isfinite(sd) or sd <= 0:
        sd = 1e-6
    return rho, sd, means


def _panel_path(last: float, mean: float, rho: float, horizon: int) -> float:
    value = float(last)
    for _ in range(int(horizon)):
        value = mean + rho * (value - mean)
    return value


def _fit_bridge(y: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """Fit ``y_t = b0 + b1*y_(t-1) + b2*x_(t-1)`` and an auxiliary AR(1) for x."""
    y = np.asarray(y, dtype=float).ravel()
    x = np.asarray(x, dtype=float).ravel()
    if y.size != x.size or y.size < MIN_BRIDGE_OBS:
        raise InsufficientHistory(f"bridge needs >= {MIN_BRIDGE_OBS} aligned observations")
    design = np.column_stack((np.ones(y.size - 1), y[:-1], x[:-1]))
    target = y[1:]
    beta, *_ = np.linalg.lstsq(design, target, rcond=None)
    residual = target - design @ beta
    sd = float(np.std(residual, ddof=1)) if residual.size > 1 else 0.0
    if not math.isfinite(sd) or sd <= 0:
        sd = max(abs(float(np.mean(y))) * 0.01, 1e-6)
    mean_x = float(np.mean(x))
    centred = x - mean_x
    denominator = float(np.dot(centred[:-1], centred[:-1]))
    phi = 0.0 if denominator <= 0 else float(np.dot(centred[:-1], centred[1:]) / denominator)
    phi = float(np.clip(phi, -0.99, 0.99))
    return beta, sd, phi, mean_x


def _rw_sd(values: np.ndarray, horizon: int) -> float:
    values = np.asarray(values, dtype=float).ravel()
    if values.size > horizon + 1:
        changes = values[horizon:] - values[:-horizon]
        sd = float(np.std(changes, ddof=1))
    else:
        sd = float(np.std(values, ddof=1)) if values.size > 1 else 0.0
    if not math.isfinite(sd) or sd <= 0:
        sd = max(abs(float(values[-1])) * 0.01, 1e-6)
    return sd


def _aligned(y: CountrySeries, x: CountrySeries) -> tuple[np.ndarray, np.ndarray]:
    y_map = {o.period: o.value for o in y.observations}
    x_map = {o.period: o.value for o in x.observations}
    common = sorted(set(y_map).intersection(x_map))
    if len(common) < MIN_BRIDGE_OBS:
        raise InsufficientHistory(f"bridge: only {len(common)} common periods")
    return (np.asarray([y_map[p] for p in common], dtype=float),
            np.asarray([x_map[p] for p in common], dtype=float))


def forecast_one(
    series: CountrySeries,
    horizon: int,
    origin,
    *,
    panel: Mapping[str, CountrySeries] | None = None,
    other: CountrySeries | None = None,
    model: str = "AR_BIC",
) -> ForecastResult:
    """One model forecast at ``origin``; raises on insufficient history or PIT violation."""
    if model not in MODEL_IDS:
        raise ValueError(f"unknown model {model!r}; expected one of {MODEL_IDS}")
    horizon = int(horizon)
    if horizon not in HORIZONS:
        raise ValueError(f"horizon {horizon} is not in {HORIZONS}")
    origin_d = as_date(origin)
    sliced = series.as_of(origin_d)
    sliced.assert_pit_clean(origin_d)
    if sliced.n_obs < _min_train(series.freq):
        raise InsufficientHistory(
            f"{series.country}/{series.target}/{series.freq}: {sliced.n_obs} observations at "
            f"{origin_d.isoformat()}, need {_min_train(series.freq)}"
        )
    values = sliced.values()
    last = float(values[-1])
    order: int | None = None
    notes: list[str] = []
    if model == "RW":
        point = last
        sd = _rw_sd(values, horizon)
        notes.append("last published value; empirical h-step change scale")
    elif model == "AR_BIC":
        order, beta, sd, _residual = _fit_ar(values)
        point = float(_ar_path(beta, order, values, horizon)[-1])
        notes.append(f"AR({order}) selected by BIC; stability projection to max|root|<=0.99")
    elif model == "PANEL_AR1":
        if not panel:
            raise InsufficientHistory("panel model needs a panel mapping")
        panel_arrays: dict[str, np.ndarray] = {}
        for name, member in panel.items():
            member_sliced = member.as_of(origin_d)
            member_sliced.assert_pit_clean(origin_d)
            panel_arrays[name] = member_sliced.values()
        rho, sd, means = _fit_panel(panel_arrays)
        if series.country not in means:
            raise InsufficientHistory(
                f"panel model: {series.country} has fewer than {MIN_PANEL_OBS} observations"
            )
        point = _panel_path(last, means[series.country], rho, horizon)
        notes.append(f"pooled AR(1), rho={rho:.4f}, country mean={means[series.country]:.4f}")
    else:  # BRIDGE
        if other is None:
            raise InsufficientHistory("bridge model needs the other country target")
        other_sliced = other.as_of(origin_d)
        other_sliced.assert_pit_clean(origin_d)
        if other_sliced.last_period != sliced.last_period:
            raise InsufficientHistory(
                "bridge requires both country targets to be published through the same period"
            )
        y_aligned, x_aligned = _aligned(sliced, other_sliced)
        beta, sd, phi, mean_x = _fit_bridge(y_aligned, x_aligned)
        y_last = float(y_aligned[-1])
        x_last = float(x_aligned[-1])
        for _ in range(horizon):
            y_next = float(beta[0] + beta[1] * y_last + beta[2] * x_last)
            x_next = float(mean_x + phi * (x_last - mean_x))
            y_last, x_last = y_next, x_next
        point = y_last
        notes.append(f"bridge with {other.target}; auxiliary AR(1) phi={phi:.4f}")
    if not math.isfinite(point):
        raise InsufficientHistory(f"{model}: non-finite forecast")
    sigma = sd * math.sqrt(horizon) if model != "RW" else sd
    lo = point - Z80 * sigma
    hi = point + Z80 * sigma
    last_period = int(sliced.last_period)  # type: ignore[arg-type]
    target_period = next_period(last_period, horizon)
    return ForecastResult(
        country=series.country,
        target=series.target,
        freq=series.freq,
        model=model,
        origin_date=origin_d,
        origin_period=last_period,
        target_period=target_period,
        horizon=horizon,
        point=float(point),
        lo80=float(min(lo, point)),
        hi80=float(max(hi, point)),
        n_obs=sliced.n_obs,
        order=order,
        pit_status=series.pit_status,
        data_status=DATA_CLASS_SYNTHETIC if series.is_synthetic else DATA_CLASS_REAL,
        notes=tuple(notes),
    )


def forecast_target(
    series: CountrySeries,
    horizon: int,
    origin,
    *,
    panel: Mapping[str, CountrySeries] | None = None,
    other: CountrySeries | None = None,
    models: Sequence[str] = MODEL_IDS,
) -> dict:
    """Forecast all requested models and record explicit per-model failure reasons."""
    origin_d = as_date(origin)
    forecasts: list[ForecastResult] = []
    status: dict[str, dict] = {}
    for model in models:
        try:
            result = forecast_one(series, horizon, origin_d, panel=panel, other=other, model=model)
        except (InsufficientHistory, PITViolationError) as exc:
            status[model] = {"status": STATUS_NOT_AVAILABLE, "reason": str(exc)}
        else:
            forecasts.append(result)
            status[model] = {"status": "OK", "reason": ""}
    return {
        "origin": origin_d.isoformat(),
        "origin_period": period_label(int(series.as_of(origin_d).last_period), series.freq)
        if series.as_of(origin_d).n_obs else None,
        "forecasts": forecasts,
        "model_status": status,
    }


# --------------------------------------------------------------------------------------
# Walk-forward OOS
# --------------------------------------------------------------------------------------
def walk_forward(
    series: CountrySeries,
    model: str,
    horizon: int,
    *,
    panel: Mapping[str, CountrySeries] | None = None,
    other: CountrySeries | None = None,
    min_train: int | None = None,
) -> list[dict]:
    """Expanding-window OOS; each origin is the release date of the last training obs."""
    horizon = int(horizon)
    if horizon not in HORIZONS:
        raise ValueError(f"horizon {horizon} is not in {HORIZONS}")
    minimum = int(min_train) if min_train is not None else _min_train(series.freq)
    observations = list(series.observations)
    rows: list[dict] = []
    for position in range(minimum - 1, len(observations) - horizon):
        origin_obs = observations[position]
        origin_date = origin_obs.release_date or period_end(origin_obs.period, series.freq)
        sliced = series.as_of(origin_date)
        if sliced.n_obs < minimum or sliced.last_period != origin_obs.period:
            continue
        panel_sliced = {name: member.as_of(origin_date) for name, member in panel.items()} if panel else None
        other_sliced = other.as_of(origin_date) if other is not None else None
        try:
            result = forecast_one(sliced, horizon, origin_date, panel=panel_sliced,
                                  other=other_sliced, model=model)
        except (InsufficientHistory, PITViolationError):
            continue
        actual_observation = observations[position + horizon]
        if actual_observation.period != origin_obs.period + horizon:
            # fail-closed: a gap must not be scored as an h-step-ahead forecast
            continue
        actual = float(actual_observation.value)
        rows.append({
            "origin_period": period_label(origin_obs.period, series.freq),
            "origin_period_index": int(origin_obs.period),
            "origin_date": origin_date.isoformat(),
            "target_period": period_label(result.target_period, series.freq),
            "target_period_index": int(result.target_period),
            "forecast": float(result.point),
            "actual": actual,
            "error": float(actual - result.point),
            "pit_status": series.pit_status,
            "data_status": DATA_CLASS_SYNTHETIC if series.is_synthetic else DATA_CLASS_REAL,
        })
    return rows


def oos_metrics(rows: Sequence[Mapping], freq: str) -> dict:
    if not rows:
        return {
            "n_origins": 0, "rmse": None, "mae": None, "bias": None,
            "status": STATUS_NOT_AVAILABLE, "first_origin": None, "last_origin": None,
        }
    errors = np.asarray([float(row["error"]) for row in rows], dtype=float)
    rmse = float(math.sqrt(float(np.mean(errors ** 2))))
    mae = float(np.mean(np.abs(errors)))
    bias = float(np.mean(errors))
    status = "OK" if len(rows) >= MIN_OOS[freq] else "INSUFFICIENT_OOS"
    return {
        "n_origins": int(len(rows)),
        "rmse": rmse,
        "mae": mae,
        "bias": bias,
        "status": status,
        "first_origin": str(rows[0]["origin_period"]),
        "last_origin": str(rows[-1]["origin_period"]),
    }


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------
def _panel_for(
    loaded: Mapping[tuple[str, str], CountrySeries],
    countries: Sequence[str],
    target: str,
    country: str,
) -> dict[str, CountrySeries]:
    own = loaded[(country, target)]
    return {
        other: loaded[(other, target)]
        for other in countries
        if loaded[(other, target)].source_class == own.source_class
    }


def _other_for(
    loaded: Mapping[tuple[str, str], CountrySeries],
    country: str,
    target: str,
) -> CountrySeries | None:
    other_target = "CPI" if target == "GDP" else "GDP"
    candidate = loaded.get((country, other_target))
    own = loaded[(country, target)]
    if candidate is None or candidate.source_class != own.source_class:
        return None
    return candidate


def _gap_reason(probes: Sequence[SourceProbe], freq: str) -> str:
    if freq == "Q":
        return ("No local quarterly PIT provider (data/external/country/<CC>_<TARGET>_Q.csv); "
                "World Bank WDI is annual; OECD/IMF quarterly vintages are not present locally. "
                "The synthetic fixture is used for pipeline verification only.")
    details = []
    for probe in probes:
        if probe.provider == "WORLD_BANK_WDI" and probe.status != "AVAILABLE":
            details.append(f"{probe.status}: {probe.detail}")
    return ("No real annual series for this country/target. "
            + ("; ".join(details) if details else "WDI cache unavailable"))


def _country_status(target_entries: Iterable[Mapping]) -> str:
    statuses = {entry["status"] for entry in target_entries}
    if statuses == {STATUS_REAL}:
        return STATUS_REAL
    if statuses == {STATUS_SYNTHETIC}:
        return STATUS_SYNTHETIC
    if STATUS_REAL in statuses and STATUS_SYNTHETIC in statuses:
        return STATUS_MIXED
    return STATUS_NOT_AVAILABLE


def build_report(
    countries: Sequence[str] = COUNTRY_IDS,
    targets: Sequence[str] = TARGET_IDS,
    horizons: Sequence[int] = HORIZONS,
    *,
    freq: str = "Q",
    origin=None,
    external_root=None,
    wb_root=None,
    allow_synthetic: bool = True,
    generated_at: str | None = None,
    models: Sequence[str] = MODEL_IDS,
) -> dict:
    """Build the aggregates-only country report (JSON-serialisable)."""
    freq = normalize_frequency(freq)
    countries = tuple(dict.fromkeys(normalize_country(c) for c in countries))
    targets = tuple(dict.fromkeys(normalize_target(t) for t in targets))
    horizons = tuple(int(h) for h in horizons)
    if not countries:
        raise ValueError("at least one country is required")
    if not targets:
        raise ValueError("at least one target is required")
    if not horizons or any(h not in HORIZONS for h in horizons):
        raise ValueError(f"horizons must be a non-empty subset of {HORIZONS}")
    models = tuple(models)
    if not models:
        raise ValueError("at least one model is required")
    if any(model not in MODEL_IDS for model in models):
        raise ValueError(f"unknown model; expected a subset of {MODEL_IDS}")

    loaded: dict[tuple[str, str], CountrySeries] = {}
    probes: list[SourceProbe] = []
    probes_by_key: dict[tuple[str, str], list[SourceProbe]] = {}
    for country in countries:
        for target in targets:
            series, series_probes = load_series(
                country, target, freq, external_root=external_root, wb_root=wb_root,
                allow_synthetic=allow_synthetic,
            )
            assert series is not None
            loaded[(country, target)] = series
            probes.extend(series_probes)
            probes_by_key[(country, target)] = series_probes

    if origin is None:
        releases = [series.max_release_date for series in loaded.values() if series.max_release_date]
        if not releases:
            raise DataNotAvailable("no observations in any requested series")
        origin_d = max(releases)
    else:
        origin_d = as_date(origin)
    if origin_d < min(series.max_release_date for series in loaded.values() if series.max_release_date):
        raise PITViolationError(f"origin {origin_d.isoformat()} precedes the first available release")

    forecast_rows: list[dict] = []
    model_status: dict[tuple[str, str], dict] = {}
    for country in countries:
        for target in targets:
            series = loaded[(country, target)]
            panel = _panel_for(loaded, countries, target, country)
            other = _other_for(loaded, country, target)
            for horizon in horizons:
                result = forecast_target(series, horizon, origin_d, panel=panel, other=other, models=models)
                for forecast in result["forecasts"]:
                    row = forecast.to_dict()
                    row["source_class"] = series.source_class
                    row["real"] = not series.is_synthetic
                    forecast_rows.append(row)
                model_status[(country, target)] = result["model_status"]

    oos_rows: list[dict] = []
    for country in countries:
        for target in targets:
            series = loaded[(country, target)]
            panel = _panel_for(loaded, countries, target, country)
            other = _other_for(loaded, country, target)
            for horizon in horizons:
                rw_metrics = None
                horizon_rows: list[dict] = []
                for model in models:
                    rows = walk_forward(series, model, horizon,
                                        panel=panel if len(panel) >= MIN_PANEL_MEMBERS else None,
                                        other=other)
                    metrics = oos_metrics(rows, freq)
                    if model == "RW":
                        rw_metrics = metrics
                    horizon_rows.append({
                        "country": country,
                        "target": target,
                        "frequency": freq,
                        "horizon": int(horizon),
                        "model": model,
                        "n_origins": metrics["n_origins"],
                        "rmse": metrics["rmse"],
                        "mae": metrics["mae"],
                        "bias": metrics["bias"],
                        "status": metrics["status"],
                        "first_origin": metrics["first_origin"],
                        "last_origin": metrics["last_origin"],
                        "rel_rmse_vs_rw": None,
                        "skill_vs_rw": None,
                        "source_class": series.source_class,
                        "real": not series.is_synthetic,
                    })
                rw_rmse = rw_metrics["rmse"] if rw_metrics else None
                for row in horizon_rows:
                    if rw_rmse and row["rmse"] is not None and rw_rmse > 0:
                        relative = float(row["rmse"] / rw_rmse)
                        row["rel_rmse_vs_rw"] = relative
                        row["skill_vs_rw"] = float(1.0 - relative)
                oos_rows.extend(horizon_rows)

    country_entries: list[dict] = []
    data_gaps: list[dict] = []
    for country in countries:
        target_entries: dict[str, dict] = {}
        target_statuses: list[dict] = []
        for target in targets:
            series = loaded[(country, target)]
            real = not series.is_synthetic
            entry = series.describe()
            entry["status"] = STATUS_REAL if real else STATUS_SYNTHETIC
            entry["reason"] = "" if real else _gap_reason(probes_by_key[(country, target)], freq)
            entry["forecast_count"] = sum(
                1 for row in forecast_rows
                if row["country"] == country and row["target"] == target
            )
            entry["model_status"] = model_status.get((country, target), {})
            target_entries[target] = entry
            target_statuses.append(entry)
            if not real:
                data_gaps.append({
                    "country": country,
                    "target": target,
                    "frequency": freq,
                    "status": "REAL_DATA_NOT_AVAILABLE",
                    "used_instead": DATA_CLASS_SYNTHETIC,
                    "reason": entry["reason"],
                })
        country_entries.append({
            "country": country,
            "name": COUNTRY_NAMES[country],
            "status": _country_status(target_statuses),
            "real": all(entry["status"] == STATUS_REAL for entry in target_statuses),
            "has_real_data": any(entry["status"] == STATUS_REAL for entry in target_statuses),
            "targets": target_entries,
        })

    real_pairs = sum(
        1 for country in countries for target in targets
        if not loaded[(country, target)].is_synthetic
    )
    synthetic_pairs = len(countries) * len(targets) - real_pairs
    real_countries = sum(1 for entry in country_entries if entry["has_real_data"])
    synthetic_countries = sum(1 for entry in country_entries if not entry["has_real_data"])
    real_quarterly_pairs = sum(
        1 for country in countries for target in targets
        if freq == "Q" and not loaded[(country, target)].is_synthetic
    )
    summary = {
        "countries_requested": len(countries),
        "country_target_pairs": len(countries) * len(targets),
        "countries_with_real_data": int(real_countries),
        "countries_synthetic": int(synthetic_countries),
        "countries_not_available": 0,
        "real_country_target_pairs": int(real_pairs),
        "synthetic_country_target_pairs": int(synthetic_pairs),
        "real_quarterly_pairs": int(real_quarterly_pairs),
        "forecasts": len(forecast_rows),
        "forecasts_ok": sum(1 for row in forecast_rows),
        "oos_rows": len(oos_rows),
        "oos_ok": sum(1 for row in oos_rows if row["status"] == "OK"),
        "models": list(models),
        "frequency": freq,
    }

    sources = [
        {
            "source_id": "WORLD_BANK",
            "name": "World Bank WDI (annual GDP growth and CPI inflation)",
            "licence": "CC BY 4.0; attribution required",
            "url": "https://databank.worldbank.org/source/world-development-indicators",
            "retrieved_at": next((s.retrieved_at for s in loaded.values() if s.source_id == "WORLD_BANK"), None),
            "used_for": "annual CN/GB series when present in the local cache",
        },
        {
            "source_id": "SYNTHETIC_FIXTURE",
            "name": "deterministic local fixture",
            "licence": "synthetic; no third-party data; not for real-country claims",
            "url": None,
            "retrieved_at": None,
            "used_for": "pipeline verification where real data is NOT_AVAILABLE",
        },
    ]

    report = {
        "schema": "amfe.country_forecast.v1",
        "report_id": "AMFE-COUNTRY-FORECAST",
        "generated_at": generated_at or utc_now(),
        "status": STATUS_RESEARCH_ONLY,
        "admission": False,
        "causal_claim": False,
        "frequency": freq,
        "frequency_label": FREQ_LABELS[freq],
        "targets": list(targets),
        "horizons": list(horizons),
        "origin": origin_d.isoformat(),
        "countries": country_entries,
        "providers": [probe.to_dict() for probe in probes],
        "forecasts": forecast_rows,
        "oos": oos_rows,
        "summary": summary,
        "data_gaps": data_gaps,
        "sources": sources,
        "limitations": _limitations(freq, real_quarterly_pairs),
        "digest_scope": {"excludes": list(DIGEST_EXCLUDES),
                         "canonicalisation": "json.dumps(sort_keys=True, separators=(',', ':'))"},
        "content_sha256": None,
    }
    checks = _report_checks(report)
    report["checks"] = checks
    if not all(value is True for key, value in checks.items() if key not in ("models",)):
        raise ReportIntegrityError(f"country report integrity failed: {checks}")
    report["content_sha256"] = content_digest(report)
    _assert_no_raw_arrays(report)
    return report


def _limitations(freq: str, real_quarterly_pairs: int) -> list[str]:
    common = [
        "RESEARCH_ONLY: no admission stage, no C1/C2/C4/C6, not investment advice.",
        "Simple models (RW, AR, pooled panel, single-equation bridge); no structural or general-equilibrium claim.",
        "80% intervals are model residual scale, not calibrated predictive distributions.",
        "Walk-forward OOS is expanding-window with h=1..4; annual samples are small (about 30 observations).",
        "Panel and bridge models use only series of the same source_class; synthetic and real series are never mixed.",
    ]
    if freq == "Q":
        return common + [
            "Real quarterly EA/CN/GB series are NOT_AVAILABLE locally: data/raw contains US series only, "
            "the WDI cache is annual, and no OECD/IMF quarterly vintage files are present.",
            f"The quarterly block is a synthetic fixture ({real_quarterly_pairs} real country-target pairs); "
            "its OOS numbers verify the pipeline, not the accuracy of forecasts for EA/CN/GB.",
            "A user can supply real quarterly PIT files in data/external/country/; the adapter then uses them.",
        ]
    return common + [
        "WDI is a final-vintage annual source: revisions and release dates are not stored, so annual OOS has "
        "revision look-ahead and is not a real-time PIT evaluation.",
        "There is no euro-area aggregate in the local WDI cache; member-state aggregation would need weights "
        "and is deliberately not attempted.",
        "Annual OOS origins are year-ends; WDI publication actually lags by months, so the timing is optimistic.",
    ]


def _report_checks(report: Mapping) -> dict:
    forecasts = report.get("forecasts", [])
    oos = report.get("oos", [])
    targets_after = all(
        int(row["target_period_index"]) > int(row.get("origin_period_index", 0))
        for row in forecasts
    ) if forecasts else True
    intervals_ordered = all(
        float(row["interval80"][0]) <= float(row["point"]) <= float(row["interval80"][1])
        for row in forecasts
    ) if forecasts else True
    finite = all(
        math.isfinite(float(row["point"])) and all(math.isfinite(float(v)) for v in row["interval80"])
        for row in forecasts
    ) if forecasts else True
    oos_finite = all(
        row["rmse"] is None or (math.isfinite(float(row["rmse"])) and math.isfinite(float(row["mae"])))
        for row in oos
    )
    return {
        "research_only": report.get("status") == STATUS_RESEARCH_ONLY,
        "admission_false": report.get("admission") is False,
        "forecast_targets_after_origin": bool(targets_after),
        "intervals_ordered": bool(intervals_ordered),
        "forecast_values_finite": bool(finite),
        "oos_metrics_finite": bool(oos_finite),
        "no_raw_series_in_report": True,
        "models": list(report.get("summary", {}).get("models", [])),
    }


def _assert_no_raw_arrays(report: Mapping) -> None:
    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in FORBIDDEN_REPORT_KEYS and path not in ("checks",):
                    raise ReportIntegrityError(f"forbidden raw-data key in report: {path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            numeric = [item for item in node if isinstance(item, (int, float)) and not isinstance(item, bool)]
            if len(numeric) > 8:
                raise ReportIntegrityError(f"numeric array of length {len(numeric)} at {path or '<root>'}")
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
    walk(report)


def _canonical_payload(report: Mapping) -> str:
    payload = {key: value for key, value in report.items() if key not in DIGEST_EXCLUDES}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_digest(report: Mapping) -> str:
    """SHA-256 of the canonical payload excluding ``generated_at`` and the digest itself."""
    return hashlib.sha256(_canonical_payload(report).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------
def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def render_markdown(report: Mapping) -> str:
    frequency = report["frequency_label"]
    lines = [
        f"# Страновой прогноз ({frequency}): EA / CN / GB",
        "",
        f"Статус: **{report['status']}**, admission=false, causal_claim=false. "
        f"Дата формирования: {report['generated_at']}; as-of данных: {report['origin']}.",
        "",
        "Это исследовательский слой для UC-06/07/08. Он не присваивает допуск и не является "
        "инвестиционной рекомендацией; синтетические ряды помечены явно.",
        "",
        "## 1. Данные и статус",
        "",
        "| Страна | Цель | Статус | Источник | PIT | Наблюдений | Последний период | Релиз |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for country in report["countries"]:
        for target in report.get("targets", TARGET_IDS):
            entry = country["targets"][target]
            lines.append(
                f"| {country['country']} | {target} | {entry['status']} | {entry['source_id']} | "
                f"{entry['pit_status']} | {entry['n_observations']} | {entry['last_period']} | "
                f"{entry['last_release'] or '—'} |"
            )
    summary = report["summary"]
    lines += [
        "",
        f"Реально оценённых стран: **{summary['countries_with_real_data']}**; "
        f"реальных страна×цель пар: **{summary['real_country_target_pairs']}**; "
        f"реальных квартальных пар: **{summary['real_quarterly_pairs']}**; "
        f"синтетических пар: **{summary['synthetic_country_target_pairs']}**.",
        "",
        "## 2. Прогнозы на 1–4 шага",
        "",
        "Точечный прогноз и 80% интервал; цель — рост ВВП или инфляция CPI в процентах.",
        "",
        "| Страна | Цель | h | Период | RW | AR_BIC | PANEL_AR1 | BRIDGE |",
        "|---|---|---:|---|---|---|---|---|",
    ]
    by_key = {
        (row["country"], row["target"], row["horizon"], row["model"]): row
        for row in report["forecasts"]
    }
    for country in report["countries"]:
        for target in report.get("targets", TARGET_IDS):
            for horizon in report.get("horizons", HORIZONS):
                if (country["country"], target, horizon, "RW") not in by_key:
                    continue
                cells = []
                period = "—"
                for model in MODEL_IDS:
                    row = by_key.get((country["country"], target, horizon, model))
                    if row is None:
                        cells.append("—")
                    else:
                        period = row["target_period"]
                        cells.append(
                            f"{_fmt(row['point'])} [{_fmt(row['interval80'][0])}; {_fmt(row['interval80'][1])}]"
                        )
                lines.append(
                    f"| {country['country']} | {target} | {horizon} | {period} | " + " | ".join(cells) + " |"
                )
    lines += [
        "",
        "## 3. Walk-forward OOS (RMSE; в скобках — отношение к RW)",
        "",
        "Расширяющееся окно, начала по датам публикации; для WDI — по концам лет (final vintage).",
        "",
        "| Страна | Цель | h | RW | AR_BIC | PANEL_AR1 | BRIDGE |",
        "|---|---|---:|---|---|---|---|",
    ]
    oos_by_key = {
        (row["country"], row["target"], row["horizon"], row["model"]): row
        for row in report["oos"]
    }
    for country in report["countries"]:
        for target in report.get("targets", TARGET_IDS):
            for horizon in report.get("horizons", HORIZONS):
                if (country["country"], target, horizon, "RW") not in oos_by_key:
                    continue
                cells = []
                for model in MODEL_IDS:
                    row = oos_by_key.get((country["country"], target, horizon, model))
                    if row is None or row["rmse"] is None:
                        cells.append("—")
                    else:
                        rel = row["rel_rmse_vs_rw"]
                        cells.append(f"{_fmt(row['rmse'])} ({_fmt(rel)})" if rel is not None else _fmt(row["rmse"]))
                lines.append(f"| {country['country']} | {target} | {horizon} | " + " | ".join(cells) + " |")
    lines += ["", "## 4. Чего нет и почему", ""]
    if report["data_gaps"]:
        for gap in report["data_gaps"]:
            lines.append(f"- **{gap['country']}/{gap['target']}**: {gap['reason']}")
    else:
        lines.append("- Реальных пропусков нет: все выбранные страны×цели покрыты реальными рядами.")
    lines += ["", "## 5. Ограничения", ""]
    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")
    lines += [
        "",
        "## 6. Воспроизведение",
        "",
        "```",
        "python tools/country_forecast.py --frequency quarterly --countries EA,CN,GB "
        "--targets GDP,CPI --horizons 1,2,3,4",
        "python tools/country_forecast.py --frequency annual --countries EA,CN,GB "
        "--targets GDP,CPI --horizons 1,2,3,4",
        "```",
        "",
        f"`content_sha256 = {report['content_sha256']}` (исключает `generated_at` и сам хеш).",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "COUNTRY_IDS",
    "TARGET_IDS",
    "FREQ_IDS",
    "HORIZONS",
    "MODEL_IDS",
    "Observation",
    "CountrySeries",
    "SourceProbe",
    "ForecastResult",
    "CountryForecastError",
    "DataNotAvailable",
    "PITViolationError",
    "InsufficientHistory",
    "ReportIntegrityError",
    "as_date",
    "period_end",
    "period_label",
    "parse_period",
    "normalize_country",
    "normalize_target",
    "normalize_frequency",
    "load_local_pit_series",
    "load_world_bank_series",
    "synthetic_series",
    "load_series",
    "forecast_one",
    "forecast_target",
    "walk_forward",
    "oos_metrics",
    "build_report",
    "render_markdown",
    "content_digest",
]
