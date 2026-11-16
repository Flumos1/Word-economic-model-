"""Typed contracts for the AMFE 2.0 finance layer (docs/145, docs/149).

The layer is a **research prototype**: it studies equities, crypto and FX with explicit
contracts, a factor model, volatility regimes and an honest out-of-sample backtest.  It never
produces trading recommendations, never claims admission and is not connected to the investment
process.  Every artefact is deterministic (no clock reads, no unseeded randomness) and carries
provenance metadata.

Public API::

    from amfe.markets_finance import (
        AssetClass, MarketDataError, ModelCard, Provenance, PriceSeries, ReturnSeries,
        synthetic_dataset, SyntheticMarketConfig,
    )
    dataset = synthetic_dataset(SyntheticMarketConfig(seed=20261116))
    prices = dataset.equities["EQ01"]
    returns = prices.returns()                 # simple returns, dates()[1:]

Contracts encoded here:

* :class:`AssetClass` - EQUITY / CRYPTO / FX / RATES;
* :class:`Provenance` - source id, dataset id, retrieval date, licence, SHA-256, data class
  (``SYNTHETIC`` for the offline generator, ``LOCAL_CSV`` for the local adapter);
* :class:`PriceSeries` - ISO dates + positive prices, strictly increasing dates, same length;
* :class:`ReturnSeries` - aligned simple/log returns with an explicit convention;
* :class:`ModelCard` - the module passport required by level L2 of the platform plan
  (inputs, outputs, data classes, tests, limitations, research status).

Math conventions (documented once, reused by every module):

* simple return ``r_t = P_t / P_{t-1} - 1``; log return ``r_t = ln(P_t / P_{t-1})``;
* the return series is one observation shorter than the price series and is aligned with
  ``dates[1:]``;
* annualisation uses ``periods_per_year`` (252 for daily equity data, 365 for crypto, 252 for FX);
* every array is validated to be finite: a NaN/inf in a contract is a hard error, never a silent
  zero.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Iterable, Sequence

import numpy as np

__all__ = [
    "AssetClass",
    "DataClass",
    "MarketDataError",
    "ContractError",
    "ModelCardError",
    "PriceSeries",
    "ReturnSeries",
    "Provenance",
    "ModelCard",
    "ModelStatus",
    "simple_returns",
    "log_returns",
    "annualise",
    "rolling_vol",
    "canonical_sha256",
    "validate_dates",
    "ensure_finite",
]


class AssetClass(str, Enum):
    """Asset class of an instrument; determines the annualisation convention."""

    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"
    FX = "FX"
    RATES = "RATES"

    @property
    def periods_per_year(self) -> int:
        return {AssetClass.EQUITY: 252, AssetClass.CRYPTO: 365,
                AssetClass.FX: 252, AssetClass.RATES: 252}[self]


class DataClass(str, Enum):
    """Provenance class of a dataset (docs/145 risk table: no future data leaks)."""

    SYNTHETIC = "SYNTHETIC"
    LOCAL_CSV = "LOCAL_CSV"
    PUBLIC_VENDOR_TEMPLATE = "PUBLIC_VENDOR_TEMPLATE"


class ModelStatus(str, Enum):
    """A finance prototype is research-only; no deployment status exists in this layer."""

    RESEARCH_ONLY = "RESEARCH_ONLY"


class MarketDataError(ValueError):
    """Invalid market data: shape/date/value violations that must fail closed."""


class ContractError(ValueError):
    """A contract object was constructed with inconsistent metadata."""


class ModelCardError(ValueError):
    """The model passport is incomplete or internally inconsistent."""


# ---------------------------------------------------------------------------------------- helpers
def ensure_finite(values: np.ndarray, name: str) -> np.ndarray:
    """Return ``values`` as a float array, raising when any element is NaN or infinite."""
    array = np.asarray(values, dtype=float)
    if array.size and not np.all(np.isfinite(array)):
        bad = int(np.size(array) - np.count_nonzero(np.isfinite(array)))
        raise MarketDataError(f"{name}: {bad} non-finite value(s) (NaN/inf are never imputed)")
    return array


def validate_dates(dates: Sequence[str], name: str = "dates") -> tuple[str, ...]:
    """Validate ISO-8601 dates: non-empty, strictly increasing, no duplicates."""
    if not dates:
        raise MarketDataError(f"{name}: the calendar is empty")
    parsed: list[date] = []
    for item in dates:
        try:
            parsed.append(date.fromisoformat(str(item)))
        except ValueError as error:
            raise MarketDataError(f"{name}: not an ISO-8601 date: {item!r}") from error
    for previous, current in zip(parsed, parsed[1:]):
        if current <= previous:
            raise MarketDataError(f"{name}: dates must be strictly increasing "
                                  f"({previous.isoformat()} -> {current.isoformat()})")
    return tuple(item.isoformat() for item in parsed)


def simple_returns(prices: np.ndarray) -> np.ndarray:
    """Simple returns ``P_t / P_{t-1} - 1``; prices must be positive and finite."""
    array = ensure_finite(prices, "prices")
    if array.size < 2:
        raise MarketDataError("prices: at least two observations are required")
    if np.any(array <= 0.0):
        raise MarketDataError("prices: non-positive price (a valid price is > 0)")
    return array[1:] / array[:-1] - 1.0


def log_returns(prices: np.ndarray) -> np.ndarray:
    """Log returns ``ln(P_t / P_{t-1})``; the exact identity ``log1p(simple)`` is guaranteed."""
    array = ensure_finite(prices, "prices")
    if array.size < 2:
        raise MarketDataError("prices: at least two observations are required")
    if np.any(array <= 0.0):
        raise MarketDataError("prices: non-positive price (a valid price is > 0)")
    return np.log(array[1:] / array[:-1])


def annualise(returns: np.ndarray, periods_per_year: int = 252, kind: str = "mean") -> float:
    """Annualise a return series (``mean`` or ``vol``); the input is not re-scaled."""
    array = ensure_finite(returns, "returns")
    if array.size == 0:
        raise MarketDataError("returns: empty series")
    if periods_per_year <= 0:
        raise MarketDataError(f"periods_per_year must be positive, got {periods_per_year}")
    if kind == "mean":
        return float(np.mean(array) * periods_per_year)
    if kind == "vol":
        if array.size < 2:
            return 0.0
        return float(np.std(array, ddof=1) * np.sqrt(periods_per_year))
    raise ContractError(f"unknown annualisation kind: {kind!r}")


def rolling_vol(returns: np.ndarray, window: int = 60, min_periods: int | None = None) -> np.ndarray:
    """Rolling sample volatility (ddof=1); the first ``window-1`` entries are NaN by design."""
    array = ensure_finite(returns, "returns")
    if window < 2:
        raise MarketDataError(f"window must be >= 2, got {window}")
    min_periods = window if min_periods is None else int(min_periods)
    if min_periods < 2 or min_periods > window:
        raise MarketDataError(f"invalid min_periods={min_periods} for window={window}")
    out = np.full(array.size, np.nan, dtype=float)
    for index in range(min_periods - 1, array.size):
        chunk = array[max(0, index - window + 1): index + 1]
        if chunk.size >= min_periods:
            out[index] = float(np.std(chunk, ddof=1)) if chunk.size > 1 else 0.0
    return out


def canonical_sha256(payload) -> str:
    """Stable SHA-256 of a JSON-serialisable object (sorted keys, UTF-8, no whitespace)."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check_array(values, name: str) -> np.ndarray:
    array = ensure_finite(values, name)
    if array.ndim != 1:
        raise MarketDataError(f"{name}: expected a 1-D array, got shape {array.shape}")
    return array


# ----------------------------------------------------------------------------------- provenance
@dataclass(frozen=True)
class Provenance:
    """Where a series comes from and under which licence; part of every artefact signature."""

    source_id: str
    dataset_id: str
    retrieved_at: str
    licence: str
    sha256: str
    data_class: DataClass = DataClass.SYNTHETIC
    pit_note: str = ""

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ContractError("provenance.source_id is empty")
        if not self.dataset_id:
            raise ContractError("provenance.dataset_id is empty")
        try:
            date.fromisoformat(self.retrieved_at)
        except ValueError as error:
            raise ContractError(f"provenance.retrieved_at is not a date: {self.retrieved_at!r}") \
                from error
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ContractError("provenance.sha256 must be a lowercase SHA-256 hex digest")
        if not isinstance(self.data_class, DataClass):
            raise ContractError("provenance.data_class must be a DataClass")

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "dataset_id": self.dataset_id,
                "retrieved_at": self.retrieved_at, "licence": self.licence,
                "sha256": self.sha256, "data_class": self.data_class.value,
                "pit_note": self.pit_note}


# --------------------------------------------------------------------------------- price series
@dataclass(frozen=True)
class PriceSeries:
    """A validated price series: ISO dates, positive prices, provenance."""

    name: str
    asset_class: AssetClass
    dates: tuple[str, ...]
    prices: np.ndarray
    provenance: Provenance
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.name:
            raise MarketDataError("PriceSeries.name is empty")
        if not isinstance(self.asset_class, AssetClass):
            raise MarketDataError("PriceSeries.asset_class must be an AssetClass")
        object.__setattr__(self, "dates", validate_dates(self.dates, f"{self.name}.dates"))
        prices = _check_array(self.prices, f"{self.name}.prices")
        if prices.size != len(self.dates):
            raise MarketDataError(f"{self.name}: {prices.size} prices for {len(self.dates)} dates")
        if prices.size < 2:
            raise MarketDataError(f"{self.name}: at least two prices are required")
        if np.any(prices <= 0.0):
            raise MarketDataError(f"{self.name}: non-positive price")
        object.__setattr__(self, "prices", prices)
        if not isinstance(self.provenance, Provenance):
            raise MarketDataError("PriceSeries.provenance must be Provenance")

    def returns(self) -> "ReturnSeries":
        """Simple returns aligned with ``dates[1:]``."""
        values = simple_returns(self.prices)
        return ReturnSeries(name=self.name, asset_class=self.asset_class,
                            dates=self.dates[1:], values=values, kind="simple",
                            provenance=self.provenance)

    def log_returns(self) -> "ReturnSeries":
        values = log_returns(self.prices)
        return ReturnSeries(name=self.name, asset_class=self.asset_class,
                            dates=self.dates[1:], values=values, kind="log",
                            provenance=self.provenance)

    def with_prices(self, prices: Iterable[float], dates: Sequence[str] | None = None) -> "PriceSeries":
        """Copy with replaced prices (a new validated object; the original is immutable)."""
        return PriceSeries(name=self.name, asset_class=self.asset_class,
                           dates=self.dates if dates is None else tuple(dates),
                           prices=np.asarray(list(prices), dtype=float),
                           provenance=self.provenance, currency=self.currency)

    def to_metadata(self) -> dict:
        return {"name": self.name, "asset_class": self.asset_class.value,
                "observations": int(self.prices.size), "first_date": self.dates[0],
                "last_date": self.dates[-1], "currency": self.currency,
                "prices_sha256": hashlib.sha256(
                    np.ascontiguousarray(self.prices, dtype=float).tobytes()).hexdigest(),
                "provenance": self.provenance.to_dict()}


@dataclass(frozen=True)
class ReturnSeries:
    """A validated return series aligned with the dates that follow the price observations."""

    name: str
    asset_class: AssetClass
    dates: tuple[str, ...]
    values: np.ndarray
    kind: str = "simple"
    provenance: Provenance | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise MarketDataError("ReturnSeries.name is empty")
        if not isinstance(self.asset_class, AssetClass):
            raise MarketDataError("ReturnSeries.asset_class must be an AssetClass")
        object.__setattr__(self, "dates", validate_dates(self.dates, f"{self.name}.dates"))
        values = _check_array(self.values, f"{self.name}.values")
        if values.size != len(self.dates):
            raise MarketDataError(f"{self.name}: {values.size} returns for {len(self.dates)} dates")
        if values.size < 1:
            raise MarketDataError(f"{self.name}: at least one return is required")
        if self.kind not in ("simple", "log"):
            raise MarketDataError(f"{self.name}: return kind must be 'simple' or 'log'")
        if np.any(values <= -1.0) and self.kind == "simple":
            raise MarketDataError(f"{self.name}: a simple return <= -100% is not admissible")
        object.__setattr__(self, "values", values)

    @property
    def periods_per_year(self) -> int:
        return self.asset_class.periods_per_year

    def annualised_mean(self) -> float:
        return annualise(self.values, self.periods_per_year, "mean")

    def annualised_vol(self) -> float:
        return annualise(self.values, self.periods_per_year, "vol")

    def slice(self, start: int, stop: int) -> "ReturnSeries":
        if not 0 <= start < stop <= self.values.size:
            raise MarketDataError(f"invalid slice [{start}:{stop}) of {self.values.size} returns")
        return ReturnSeries(name=self.name, asset_class=self.asset_class,
                            dates=self.dates[start:stop], values=self.values[start:stop],
                            kind=self.kind, provenance=self.provenance)

    def to_metadata(self) -> dict:
        return {"name": self.name, "asset_class": self.asset_class.value, "kind": self.kind,
                "observations": int(self.values.size), "first_date": self.dates[0],
                "last_date": self.dates[-1],
                "annualised_mean": self.annualised_mean(),
                "annualised_vol": self.annualised_vol(),
                "returns_sha256": hashlib.sha256(
                    np.ascontiguousarray(self.values, dtype=float).tobytes()).hexdigest()}


# ------------------------------------------------------------------------------------- model card
@dataclass(frozen=True)
class ModelCard:
    """Module passport (platform level L2): inputs, outputs, data classes, tests, limitations."""

    model_id: str
    version: str
    kind: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    data_classes: tuple[DataClass, ...]
    limitations: tuple[str, ...]
    tests: tuple[str, ...]
    status: ModelStatus = ModelStatus.RESEARCH_ONLY
    deterministic: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ModelCardError("model_id is empty")
        if len(self.version.split(".")) < 2:
            raise ModelCardError(f"version must look like MAJOR.MINOR, got {self.version!r}")
        if not self.inputs or not self.outputs:
            raise ModelCardError("inputs and outputs must be non-empty")
        if not self.limitations:
            raise ModelCardError("a model card without limitations is not admissible (docs/145)")
        if not self.tests:
            raise ModelCardError("a model card must name its tests")
        if any(not isinstance(item, DataClass) for item in self.data_classes):
            raise ModelCardError("data_classes must contain DataClass values")
        if not isinstance(self.status, ModelStatus):
            raise ModelCardError("status must be a ModelStatus")

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "version": self.version, "kind": self.kind,
                "status": self.status.value, "deterministic": bool(self.deterministic),
                "inputs": list(self.inputs), "outputs": list(self.outputs),
                "data_classes": [item.value for item in self.data_classes],
                "limitations": list(self.limitations), "tests": list(self.tests),
                "notes": self.notes}

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())
