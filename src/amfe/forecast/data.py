"""Point-in-time data access for the AMFE baseline forecast models.

The module reads the local, git-ignored FRED/ALFRED files (``data/raw/<SERIES>/latest.csv``
and ``data/raw/<SERIES>/vintages.zip``) and builds the protocol-v4 targets T1 (CPI y/y),
T2 (real GDP growth, q/q annualised) and T3 (NBER recession event).

Design rules
------------
* Only the standard library and numpy are used, so the suite runs with the system
  ``python`` from the repository root (the project default interpreter has numpy but
  no pandas).
* Point-in-time (PIT) access is strict.  ``available_series`` / ``available_data`` never
  return an observation after the origin: vintage-backed series use the latest ALFRED
  vintage column whose vintage date is on or before the origin; latest-tape series are
  truncated to the origin minus the series' publication lag.  A violation raises
  :class:`LookAheadError`, and ``DataContext`` re-checks every slice it receives.
* Data classes follow protocol v4 ``data_depth.classification_rule``:
  ``REAL_TIME`` when every input series comes from the vintage available at the origin,
  ``PSEUDO_REAL_TIME`` when the latest tape is truncated instead.
* The local vintage grid is sparse (10 year-end vintages per series, 1996-12..2020-12).
  Between two vintages the model sees the older vintage, so the information set can be
  stale by several years (a 2019 origin uses the 2013 vintage in this local sample).
  That is a data-depth limitation, not look-ahead; the target is still computed on the
  protocol calendar and the effective number of steps from the last published
  observation is reported by every forecast.
* T3 labels come from the latest USREC file (the automated representation of the NBER
  chronology), but a label enters model estimation only when the NBER announcement that
  covers it was published before the origin (protocol v4 ``data_depth.nber_rule``).  The
  announcement dates come from ``protocols/nber_dates.json``; the label cutoff is the
  latest trough whose announcement predates the origin, minus the horizon.  A fixed lag is
  used only as a fallback when the announcement file is unavailable.
"""

from __future__ import annotations

import calendar
import csv
import os
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = ROOT / "data" / "raw"

#: T10Y3M is daily and is not used by any R3 model (PROBIT uses GS10-TB3MS), so it is not loaded.
MONTHLY_SERIES = ("CPIAUCSL", "CPILFESL", "UNRATE", "GS10", "TB3MS", "USREC")
QUARTERLY_SERIES = ("GDPC1",)
ALL_SERIES = MONTHLY_SERIES + QUARTERLY_SERIES

#: Series that the local repository stores with an ALFRED vintage archive.
VINTAGE_CAPABLE = frozenset(("CPIAUCSL", "CPILFESL", "GDPC1", "UNRATE", "GS10", "TB3MS"))

#: Approximate publication lag in months, used only for the latest-tape (PSEUDO_REAL_TIME)
#: fallback: an observation is available at the origin only ``lag`` months after its period.
RELEASE_LAG_MONTHS = {
    "CPIAUCSL": 1,
    "CPILFESL": 1,
    "UNRATE": 1,
    "GS10": 1,
    "TB3MS": 1,
    "GDPC1": 3,
    "USREC": 12,
}

#: Fallback only: if protocols/nber_dates.json is unavailable, the chronology is treated as
#: known 12 months after the month.  With the file present the announcement dates are used.
NBER_ANNOUNCEMENT_LAG_MONTHS = 12
#: Recorded NBER announcements (peak/trough and the announcement dates), committed metadata.
NBER_DATES_FILE = ROOT / "protocols" / "nber_dates.json"
_NBER_REFERENCE_CACHE: dict[str, list] = {}

DATA_CLASS_REAL_TIME = "REAL_TIME"
DATA_CLASS_PSEUDO = "PSEUDO_REAL_TIME"

TARGET_IDS = ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION")
TARGET_SPEC = {
    "T1_CPI_YOY": {"freq": "M", "horizons": (1, 3, 6, 12), "window": 180, "unit": "percent y/y"},
    "T2_GDP_GROWTH": {"freq": "Q", "horizons": (0, 1, 2, 4), "window": 60, "unit": "percent annualised"},
    "T3_RECESSION": {"freq": "M", "horizons": (3, 6, 12), "window": 240, "unit": "event probability"},
}
T1_SERIES = ("CPIAUCSL", "CPILFESL")

MODE_AUTO = "auto"
MODE_VINTAGE = "vintage"
MODE_LATEST = "latest"


class ForecastDataError(RuntimeError):
    """Base class for data-layer errors."""


class SeriesUnavailable(ForecastDataError):
    """The requested series is not present locally or has no usable vintage."""


class OriginOutOfRangeError(ForecastDataError):
    """The origin is outside the locally available data span."""


class LookAheadError(ForecastDataError):
    """A PIT contract failed: an observation after the origin would be exposed."""


# --------------------------------------------------------------------------------------
# Calendar helpers: month index = year*12 + month-1, quarter index = year*4 + quarter-1
# --------------------------------------------------------------------------------------
def as_date(value) -> date:
    """Coerce ``date`` / ``datetime`` / ISO string (``YYYY-MM-DD`` or ``YYYY-MM``) to date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    if len(text) == 7:
        text += "-01"
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ForecastDataError(f"cannot parse date {value!r}") from exc


def month_end_of(value) -> date:
    d = as_date(value)
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(value, months: int) -> date:
    d = month_end_of(value)
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, calendar.monthrange(total // 12, total % 12 + 1)[1])


def month_index(value) -> int:
    d = as_date(value) if not isinstance(value, int) else value
    if isinstance(d, int):
        return d
    return d.year * 12 + (d.month - 1)


def month_start_date(index: int) -> date:
    return date(index // 12, index % 12 + 1, 1)


def month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def load_nber_reference_dates(path=None) -> list:
    """The recorded NBER reference dates with announcement dates (committed metadata)."""
    path = Path(path) if path is not None else NBER_DATES_FILE
    key = str(path.resolve())
    if key not in _NBER_REFERENCE_CACHE:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        _NBER_REFERENCE_CACHE[key] = list(data.get("reference_dates", []))
    return _NBER_REFERENCE_CACHE[key]


def nber_label_cutoff(origin, path=None) -> int | None:
    """Last month whose complete recession label is published before the origin.

    Conservative reading of protocol v4 ``data_depth.nber_rule``: a label enters model
    estimation only when the announcement that covers it predates the origin.  The cutoff
    is the latest NBER trough whose announcement date is before the origin; labels for
    events ending after that trough are not used.  ``None`` when the file is unavailable,
    so callers can fall back to a fixed lag.
    """
    try:
        refs = load_nber_reference_dates(path)
    except (OSError, ValueError):
        return None
    origin_d = month_end_of(as_date(origin))
    cutoff = None
    for ref in refs:
        trough = ref.get("trough")
        announced = ref.get("trough_announcement")
        if not trough or not announced:
            continue
        if date.fromisoformat(announced) < origin_d:
            month = month_index(trough)
            cutoff = month if cutoff is None else max(cutoff, month)
    return cutoff


def quarter_index(value) -> int:
    d = as_date(value)
    return d.year * 4 + (d.month - 1) // 3


def quarter_end_month(index: int) -> int:
    """Last month index (year*12+month-1) of the quarter with this quarter index."""
    return (index // 4) * 12 + (index % 4) * 3 + 2


def quarter_label(index: int) -> str:
    return f"{index // 4}Q{index % 4 + 1}"


def quarter_start_month(index: int) -> int:
    return (index // 4) * 12 + (index % 4) * 3


# --------------------------------------------------------------------------------------
# Series container
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Series:
    """A univariate series indexed by integer period ordinals (months or quarters)."""

    name: str
    freq: str  # "M" or "Q"
    index: np.ndarray
    values: np.ndarray
    source: str = ""
    vintage_date: date | None = None

    def __post_init__(self):
        idx = np.asarray(self.index, dtype=np.int64).ravel()
        vals = np.asarray(self.values, dtype=float).ravel()
        if idx.size != vals.size:
            raise ForecastDataError(f"{self.name}: index/values length mismatch")
        if idx.size > 1 and np.any(np.diff(idx) <= 0):
            raise ForecastDataError(f"{self.name}: index must be strictly increasing")
        if self.freq not in ("M", "Q"):
            raise ForecastDataError(f"{self.name}: unknown frequency {self.freq!r}")
        object.__setattr__(self, "index", idx)
        object.__setattr__(self, "values", vals)

    # -- basic accessors ---------------------------------------------------------------
    @property
    def first_index(self) -> int | None:
        finite = np.isfinite(self.values)
        if not finite.any():
            return None
        return int(self.index[np.argmax(finite)])

    @property
    def last_index(self) -> int | None:
        finite = np.isfinite(self.values)
        if not finite.any():
            return None
        return int(self.index[len(finite) - 1 - np.argmax(finite[::-1])])

    def __len__(self) -> int:
        return int(self.index.size)

    def value_at(self, period: int) -> float | None:
        pos = int(np.searchsorted(self.index, period))
        if pos < self.index.size and int(self.index[pos]) == int(period):
            value = float(self.values[pos])
            return value if np.isfinite(value) else None
        return None

    def values_dict(self) -> dict[int, float]:
        return {
            int(p): float(v)
            for p, v in zip(self.index, self.values)
            if np.isfinite(v)
        }

    def finite_index_values(self) -> tuple[np.ndarray, np.ndarray]:
        mask = np.isfinite(self.values)
        return self.index[mask], self.values[mask]

    def trimmed_to(self, period: int) -> "Series":
        keep = self.index <= int(period)
        return Series(
            self.name,
            self.freq,
            self.index[keep],
            self.values[keep],
            source=self.source,
            vintage_date=self.vintage_date,
        )

    def last_observation_date(self) -> date | None:
        idx = self.last_index
        if idx is None:
            return None
        return month_start_date(idx) if self.freq == "M" else date(idx // 4, (idx % 4) * 3 + 1, 1)

    def period_label(self, period: int | None = None) -> str:
        period = self.last_index if period is None else period
        if period is None:
            return "none"
        return month_label(period) if self.freq == "M" else quarter_label(period)


@dataclass(frozen=True)
class SeriesMeta:
    name: str
    mode: str
    data_class: str
    vintage_date: date | None = None
    release_lag_months: int = 0
    last_observation: int | None = None


def _trim_leading_trailing_nan(index: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    finite = np.isfinite(values)
    if not finite.any():
        return index[:0], values[:0]
    first = int(np.argmax(finite))
    last = len(finite) - 1 - int(np.argmax(finite[::-1]))
    return index[first : last + 1], values[first : last + 1]


def _dedupe_last(index: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Keep the last value for duplicate period ordinals (defensive for odd files)."""
    if index.size < 2 or np.all(np.diff(index) > 0):
        return index, values
    keep: dict[int, float] = {}
    for period, value in zip(index, values):
        keep[int(period)] = float(value)
    ordered = sorted(keep)
    return np.asarray(ordered, dtype=np.int64), np.asarray([keep[p] for p in ordered], dtype=float)


def _parse_float(cell: str) -> float:
    cell = (cell or "").strip()
    if cell in ("", ".", "NA", "nan", "NaN"):
        return float("nan")
    try:
        return float(cell)
    except ValueError:
        return float("nan")


def _read_latest_rows(path: Path, freq: str) -> tuple[np.ndarray, np.ndarray]:
    periods: list[int] = []
    values: list[float] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise SeriesUnavailable(f"empty file: {path}")
        for row in reader:
            if len(row) < 2:
                continue
            try:
                d = date.fromisoformat(row[0].strip()[:10])
            except ValueError:
                continue
            periods.append(month_index(d) if freq == "M" else quarter_index(d))
            values.append(_parse_float(row[1]))
    if not periods:
        raise SeriesUnavailable(f"no observations in {path}")
    idx, vals = _dedupe_last(np.asarray(periods, dtype=np.int64), np.asarray(values, dtype=float))
    idx, vals = _trim_leading_trailing_nan(idx, vals)
    return idx, vals


@lru_cache(maxsize=None)
def _latest_cached(path_str: str, mtime_ns: int) -> tuple[tuple[int, ...], tuple[float, ...]]:
    freq = "Q" if Path(path_str).parent.name in QUARTERLY_SERIES else "M"
    idx, vals = _read_latest_rows(Path(path_str), freq)
    return tuple(int(v) for v in idx), tuple(float(v) for v in vals)


def _data_root(data_root=None) -> Path:
    if data_root is not None:
        return Path(data_root)
    env = os.environ.get("AMFE_DATA_ROOT")
    return Path(env) if env else DEFAULT_DATA_ROOT


def load_latest(name: str, data_root=None) -> Series:
    """Load the downloaded latest tape of ``name`` (look-ahead is *not* applied here)."""
    path = _data_root(data_root) / name / "latest.csv"
    if not path.exists():
        raise SeriesUnavailable(f"latest.csv not found for {name}: {path}")
    mtime = path.stat().st_mtime_ns
    idx, vals = _latest_cached(str(path), mtime)
    freq = "Q" if name in QUARTERLY_SERIES else "M"
    return Series(name, freq, np.asarray(idx, dtype=np.int64), np.asarray(vals, dtype=float), source=str(path))


@lru_cache(maxsize=None)
def _vintages_cached(path_str: str, mtime_ns: int) -> dict[date, tuple[tuple[int, ...], tuple[float, ...]]]:
    freq = "Q" if Path(path_str).parent.name in QUARTERLY_SERIES else "M"
    path = Path(path_str)
    result: dict[date, tuple[tuple[int, ...], tuple[float, ...]]] = {}
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.namelist() if m.lower().endswith(".csv")]
        if not members:
            raise SeriesUnavailable(f"no vintage CSV inside {path}")
        text = archive.read(members[0]).decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    if len(rows) < 2:
        raise SeriesUnavailable(f"empty vintage CSV inside {path}")
    header = rows[0]
    periods = [_row_period(row[0], freq) for row in rows[1:] if row and row[0].strip()]
    keep_rows = [row for row in rows[1:] if row and row[0].strip()]
    for col, column_name in enumerate(header[1:], start=1):
        stamp = column_name.rsplit("_", 1)[-1]
        try:
            vintage_date = date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
        except (ValueError, IndexError):
            continue
        vals = np.asarray([_parse_float(row[col] if col < len(row) else "") for row in keep_rows], dtype=float)
        idx_arr = np.asarray([p for p in periods if p is not None], dtype=np.int64)
        if idx_arr.size != vals.size:
            continue
        idx_arr, vals = _trim_leading_trailing_nan(idx_arr, vals)
        result[vintage_date] = (tuple(int(v) for v in idx_arr), tuple(float(v) for v in vals))
    return result


def _row_period(text: str, freq: str) -> int | None:
    try:
        d = date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None
    return month_index(d) if freq == "M" else quarter_index(d)


def load_vintages(name: str, data_root=None) -> dict[date, Series]:
    """Load every ALFRED vintage column available locally for ``name``."""
    path = _data_root(data_root) / name / "vintages.zip"
    if not path.exists():
        raise SeriesUnavailable(f"vintages.zip not found for {name}: {path}")
    cached = _vintages_cached(str(path), path.stat().st_mtime_ns)
    freq = "Q" if name in QUARTERLY_SERIES else "M"
    return {
        vintage_date: Series(
            name,
            freq,
            np.asarray(idx, dtype=np.int64),
            np.asarray(vals, dtype=float),
            source=f"{path}:{vintage_date.isoformat()}",
            vintage_date=vintage_date,
        )
        for vintage_date, (idx, vals) in cached.items()
    }


def vintage_as_of(name: str, origin, data_root=None) -> tuple[Series, date] | None:
    """Latest vintage column of ``name`` whose vintage date is on or before the origin."""
    if name not in VINTAGE_CAPABLE:
        return None
    try:
        vintages = load_vintages(name, data_root)
    except SeriesUnavailable:
        return None
    origin_d = as_date(origin)
    usable = [d for d in vintages if d <= origin_d]
    if not usable:
        return None
    vintage_date = max(usable)
    return vintages[vintage_date], vintage_date


# --------------------------------------------------------------------------------------
# PIT rules
# --------------------------------------------------------------------------------------
def _origin_cutoff(name: str, origin, lag_months: int = 0) -> int:
    """Largest period ordinal of ``name`` with observation date + lag on or before the origin."""
    om = month_index(origin)
    if name in QUARTERLY_SERIES:
        limit = om - int(lag_months)
        q = max(0, (limit // 3) - 1)
        while quarter_end_month(q + 1) <= limit:
            q += 1
        while q > 0 and quarter_end_month(q) > limit:
            q -= 1
        return q
    return om - int(lag_months)


def assert_no_lookahead(series: Series, origin) -> None:
    """Raise :class:`LookAheadError` if ``series`` holds an observation after ``origin``."""
    om = month_index(origin)
    if series.freq == "Q":
        if series.index.size and quarter_end_month(int(series.index[-1])) > om:
            raise LookAheadError(
                f"{series.name}: observation {quarter_label(int(series.index[-1]))} after origin {month_label(om)}"
            )
    elif series.index.size and int(series.index[-1]) > om:
        raise LookAheadError(
            f"{series.name}: observation {month_label(int(series.index[-1]))} after origin {month_label(om)}"
        )


def latest_tape_bounds(data_root=None) -> tuple[int, int]:
    """First and last month index covered by the locally downloaded latest tapes."""
    first: int | None = None
    last: int | None = None
    root = _data_root(data_root)
    for name in ALL_SERIES:
        if name == "USREC":
            # the NBER chronology reaches back to 1854; it does not define the economic data span
            continue
        try:
            series = load_latest(name, root)
        except SeriesUnavailable:
            continue
        if series.first_index is not None:
            first_i = series.first_index if series.freq == "M" else quarter_end_month(series.first_index)
            last_i = series.last_index if series.freq == "M" else quarter_end_month(series.last_index)
            first = first_i if first is None else min(first, first_i)
            last = last_i if last is None else max(last, last_i)
    if first is None or last is None:
        raise SeriesUnavailable("no latest files under data/raw")
    return first, last


def validate_origin(origin, data_root=None) -> date:
    """Normalise ``origin`` to a month end and reject origins outside the data span."""
    origin_d = month_end_of(origin)
    first, last = latest_tape_bounds(data_root)
    om = month_index(origin_d)
    if om < first:
        raise OriginOutOfRangeError(
            f"origin {origin_d.isoformat()} is before the first observation ({month_label(first)})"
        )
    if om > last:
        raise OriginOutOfRangeError(
            f"origin {origin_d.isoformat()} is after the last observation ({month_label(last)})"
        )
    return origin_d


def available_series(name: str, origin, mode: str = MODE_AUTO, data_root=None) -> tuple[Series, SeriesMeta]:
    """Return the information set of one series available at ``origin``.

    ``mode``: ``auto`` uses the vintage when one exists locally and falls back to the
    truncated latest tape; ``vintage`` requires a vintage; ``latest`` always uses the
    truncated latest tape (PSEUDO_REAL_TIME).
    """
    if mode not in (MODE_AUTO, MODE_VINTAGE, MODE_LATEST):
        raise ValueError(f"unknown data mode {mode!r}")
    origin_d = month_end_of(origin)
    lag = RELEASE_LAG_MONTHS.get(name, 0)
    if name in VINTAGE_CAPABLE and mode in (MODE_AUTO, MODE_VINTAGE):
        got = vintage_as_of(name, origin_d, data_root)
        if got is not None:
            series, vintage_date = got
            cutoff = _origin_cutoff(name, origin_d, 0)
            series = series.trimmed_to(cutoff)
            assert_no_lookahead(series, origin_d)
            meta = SeriesMeta(
                name=name,
                mode=MODE_VINTAGE,
                data_class=DATA_CLASS_REAL_TIME,
                vintage_date=vintage_date,
                release_lag_months=0,
                last_observation=series.last_index,
            )
            return series, meta
        if mode == MODE_VINTAGE:
            raise SeriesUnavailable(
                f"{name}: no ALFRED vintage on or before {origin_d.isoformat()} in the local archive"
            )
    try:
        series = load_latest(name, data_root)
    except SeriesUnavailable:
        raise
    cutoff = _origin_cutoff(name, origin_d, lag)
    series = series.trimmed_to(cutoff)
    assert_no_lookahead(series, origin_d)
    meta = SeriesMeta(
        name=name,
        mode=MODE_LATEST,
        data_class=DATA_CLASS_PSEUDO,
        vintage_date=None,
        release_lag_months=lag,
        last_observation=series.last_index,
    )
    return series, meta


def available_data(origin, names=None, mode: str = MODE_AUTO, data_root=None) -> dict[str, Series]:
    """All requested series as known at ``origin``; strictly no observation after it."""
    origin_d = month_end_of(origin)
    slices: dict[str, Series] = {}
    for name in names or ALL_SERIES:
        try:
            series, _meta = available_series(name, origin_d, mode=mode, data_root=data_root)
        except SeriesUnavailable:
            continue
        assert_no_lookahead(series, origin_d)
        slices[name] = series
    return slices


# --------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------
def cpi_yoy(levels: Series) -> Series:
    """``100*(CPI_t/CPI_(t-12)-1)``, monthly."""
    if levels.freq != "M":
        raise ForecastDataError("cpi_yoy expects a monthly level series")
    lookup = {int(p): float(v) for p, v in zip(levels.index, levels.values)}
    out_idx: list[int] = []
    out_vals: list[float] = []
    for period, value in zip(levels.index, levels.values):
        prev = lookup.get(int(period) - 12)
        if prev is None or not np.isfinite(prev) or prev == 0 or not np.isfinite(value):
            continue
        out_idx.append(int(period))
        out_vals.append(100.0 * (float(value) / prev - 1.0))
    return Series(
        f"{levels.name}_YOY",
        "M",
        np.asarray(out_idx, dtype=np.int64),
        np.asarray(out_vals, dtype=float),
        source=f"{levels.source}|yoy",
        vintage_date=levels.vintage_date,
    )


def gdp_growth(levels: Series) -> Series:
    """``400*ln(GDP_q/GDP_(q-1))``, quarterly annualised."""
    if levels.freq != "Q":
        raise ForecastDataError("gdp_growth expects a quarterly level series")
    lookup = {int(p): float(v) for p, v in zip(levels.index, levels.values)}
    out_idx: list[int] = []
    out_vals: list[float] = []
    for period, value in zip(levels.index, levels.values):
        prev = lookup.get(int(period) - 1)
        if prev is None or not np.isfinite(prev) or prev <= 0 or not np.isfinite(value):
            continue
        out_idx.append(int(period))
        out_vals.append(400.0 * float(np.log(float(value) / prev)))
    return Series(
        f"{levels.name}_GROWTH",
        "Q",
        np.asarray(out_idx, dtype=np.int64),
        np.asarray(out_vals, dtype=float),
        source=f"{levels.source}|growth",
        vintage_date=levels.vintage_date,
    )


def usrec_event(usrec: Series, start_month: int, horizon: int) -> int | None:
    """1 if at least one USREC recession month lies in ``(start_month, start_month+h]``.

    Returns ``None`` when the chronology is incomplete for the interval (a missing month),
    so a caller can skip an unresolved label instead of reading it as 0.
    """
    values = usrec.values_dict()
    for step in range(1, horizon + 1):
        month = int(start_month) + step
        if month not in values:
            return None
        if values[month] >= 0.5:
            return 1
    return 0


def recession_window_labels(usrec: Series, start_month: int, horizon: int) -> int | None:
    """Alias of :func:`usrec_event` used by the T3 models."""
    return usrec_event(usrec, start_month, horizon)


def target_period(target: str, origin, horizon: int) -> tuple[str, int]:
    """Calendar period of the target under protocol v4.

    T1: the y/y inflation of month ``t-1+h`` (``t`` = origin month).
    T2: real GDP growth of quarter ``Q(origin)+h``.
    T3: the event window starts at the origin month ``t``.
    """
    if target not in TARGET_SPEC:
        raise ValueError(f"unknown target {target!r}")
    if int(horizon) not in TARGET_SPEC[target]["horizons"]:
        raise ValueError(f"horizon {horizon} is not in the protocol grid for {target}")
    origin_d = month_end_of(origin)
    if target == "T1_CPI_YOY":
        return "M", month_index(origin_d) - 1 + int(horizon)
    if target == "T2_GDP_GROWTH":
        return "Q", quarter_index(origin_d) + int(horizon)
    return "M", month_index(origin_d)


def quarter_average(monthly: Series, quarter: int) -> float | None:
    """Average of the three monthly values of a quarter; ``None`` if any month is missing."""
    start = quarter_start_month(quarter)
    total = 0.0
    for offset in range(3):
        value = monthly.value_at(start + offset)
        if value is None:
            return None
        total += value
    return total / 3.0


def quarter_average_series(monthly: Series) -> Series:
    """Quarterly averages of a monthly series (only quarters with all three months)."""
    out_idx: list[int] = []
    out_vals: list[float] = []
    first_q = quarter_index(month_start_date(monthly.first_index)) if monthly.first_index is not None else 0
    last_q = quarter_index(month_start_date(monthly.last_index)) if monthly.last_index is not None else -1
    for q in range(first_q, last_q + 1):
        value = quarter_average(monthly, q)
        if value is not None:
            out_idx.append(q)
            out_vals.append(value)
    return Series(
        f"{monthly.name}_QAVG",
        "Q",
        np.asarray(out_idx, dtype=np.int64),
        np.asarray(out_vals, dtype=float),
        source=f"{monthly.source}|qavg",
        vintage_date=monthly.vintage_date,
    )


def align_series(*series: Series) -> tuple[np.ndarray, list[np.ndarray], str]:
    """Intersect the indices of several series; return common index and value vectors."""
    if not series:
        raise ValueError("align_series needs at least one series")
    freq = series[0].freq
    if any(s.freq != freq for s in series):
        raise ValueError("align_series cannot mix monthly and quarterly series")
    maps = [s.values_dict() for s in series]
    common = sorted(set(maps[0]).intersection(*(set(m) for m in maps[1:])))
    index = np.asarray(common, dtype=np.int64)
    values = [np.asarray([m[p] for p in common], dtype=float) for m in maps]
    return index, values, freq


def target_actual_series(target: str, series_id: str | None = None, data_root=None) -> Series:
    """Latest-tape (final) target series, used to score forecasts (not for estimation)."""
    if target == "T1_CPI_YOY":
        sid = series_id or "CPIAUCSL"
        if sid not in T1_SERIES:
            raise ValueError(f"{sid} is not a T1 series")
        return cpi_yoy(load_latest(sid, data_root))
    if target == "T2_GDP_GROWTH":
        return gdp_growth(load_latest("GDPC1", data_root))
    if target == "T3_RECESSION":
        return load_latest("USREC", data_root)
    raise ValueError(f"unknown target {target!r}")


def target_actual(target: str, origin, horizon: int, series_id: str | None = None, data_root=None):
    """Realised target value from the latest tape (``None`` if not published yet)."""
    series = target_actual_series(target, series_id, data_root)
    if target == "T3_RECESSION":
        return usrec_event(series, month_index(origin), int(horizon))
    _freq, period = target_period(target, origin, horizon)
    return series.value_at(period)


# --------------------------------------------------------------------------------------
# Point-in-time context handed to the models
# --------------------------------------------------------------------------------------
@dataclass
class DataContext:
    """The information set of all model inputs at one origin."""

    origin: date
    series: dict[str, Series]
    meta: dict[str, SeriesMeta] = field(default_factory=dict)
    requested_mode: str = MODE_AUTO
    data_class: str = field(default=DATA_CLASS_PSEUDO, init=False)

    def __post_init__(self) -> None:
        origin_d = month_end_of(self.origin)
        object.__setattr__(self, "origin", origin_d)
        if not self.series:
            raise ForecastDataError("DataContext has no series")
        for series in self.series.values():
            assert_no_lookahead(series, origin_d)
        vintage_capable_present = [n for n in VINTAGE_CAPABLE if n in self.series]
        latest_present = [
            n for n in vintage_capable_present if self.meta.get(n, SeriesMeta(n, MODE_LATEST, DATA_CLASS_PSEUDO)).mode == MODE_LATEST
        ]
        if vintage_capable_present:
            object.__setattr__(
                self,
                "data_class",
                DATA_CLASS_PSEUDO if latest_present else DATA_CLASS_REAL_TIME,
            )
        else:
            object.__setattr__(self, "data_class", DATA_CLASS_PSEUDO)

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_local(
        cls,
        origin,
        mode: str = MODE_AUTO,
        data_root=None,
        names=None,
    ) -> "DataContext":
        origin_d = validate_origin(origin, data_root)
        slices: dict[str, Series] = {}
        metas: dict[str, SeriesMeta] = {}
        for name in names or ALL_SERIES:
            try:
                series, meta = available_series(name, origin_d, mode=mode, data_root=data_root)
            except SeriesUnavailable:
                continue
            slices[name] = series
            metas[name] = meta
        if not any(series.last_index is not None for series in slices.values()):
            raise OriginOutOfRangeError(f"no observations available at origin {origin_d.isoformat()}")
        return cls(origin=origin_d, series=slices, meta=metas, requested_mode=mode)

    # -- accessors ---------------------------------------------------------------------
    @property
    def origin_month(self) -> int:
        return month_index(self.origin)

    @property
    def origin_quarter(self) -> int:
        return quarter_index(self.origin)

    def require(self, name: str) -> Series:
        if name not in self.series or self.series[name].last_index is None:
            raise SeriesUnavailable(f"{name} is not available at origin {self.origin.isoformat()}")
        return self.series[name]

    def has(self, name: str) -> bool:
        return name in self.series and self.series[name].last_index is not None

    def vintage_dates(self) -> dict[str, str | None]:
        return {
            name: (meta.vintage_date.isoformat() if meta.vintage_date else None)
            for name, meta in self.meta.items()
        }

    def last_observation_label(self, name: str) -> str:
        series = self.require(name)
        return series.period_label()

    def target_series(self, target: str, series_id: str | None = None) -> Series:
        if target == "T1_CPI_YOY":
            sid = series_id or "CPIAUCSL"
            if sid not in T1_SERIES:
                raise ValueError(f"{sid} is not a T1 series")
            return cpi_yoy(self.require(sid))
        if target == "T2_GDP_GROWTH":
            return gdp_growth(self.require("GDPC1"))
        if target == "T3_RECESSION":
            return self.require("USREC")
        raise ValueError(f"unknown target {target!r}")

    def last_target_index(self, target: str, series_id: str | None = None) -> int:
        series = self.target_series(target, series_id)
        last = series.last_index
        if last is None:
            raise SeriesUnavailable(f"{target}: no target observations available at the origin")
        return int(last)

    def steps_to_target(self, target: str, horizon: int, series_id: str | None = None) -> int:
        """Number of periods between the last published target value and the target period."""
        _freq, period = target_period(target, self.origin, horizon)
        last = self.last_target_index(target, series_id)
        steps = int(period) - int(last)
        if steps < 1:
            raise ForecastDataError(
                f"{target} h={horizon}: target period is not after the last published value "
                f"(target={period}, last={last}); inconsistent origin/horizon"
            )
        return steps

    def usrec_labels(self) -> Series:
        return self.require("USREC")

    def describe(self) -> dict:
        return {
            "origin": self.origin.isoformat(),
            "data_class": self.data_class,
            "requested_mode": self.requested_mode,
            "vintage_dates": self.vintage_dates(),
            "last_observations": {
                name: series.period_label() for name, series in self.series.items() if series.last_index is not None
            },
        }
