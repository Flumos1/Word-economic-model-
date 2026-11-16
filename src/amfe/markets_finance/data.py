"""Deterministic synthetic market data for the finance prototype (offline by design).

The generator never touches the network: it produces equities, crypto, FX pairs and rates with a
known factor structure, so the factor model, the regime model, the FX/rates block and the OOS
backtest can be tested against a data-generating process (DGP) whose parameters are known.

DGP (one draw per seed, no clock, no unseeded randomness):

* five factors - ``MARKET``, ``VALUE``, ``MOMENTUM``, ``QUALITY``, ``SIZE`` - with constant means
  and vols; ``MARKET`` dominates, the others are small long-short premia;
* characteristics follow independent AR(1) processes (persistent, so value/momentum/size sorts are
  not pure noise);
* equity returns ``r_it = alpha_i + sum_k beta_ik f_kt + eps_it``; crypto returns use the same
  factor structure with a higher market beta and a much higher idiosyncratic vol; FX returns are a
  rate differential plus noise (a *reduced-form* FX block, not a pricing model);
* rates are AR(1) around fixed levels; FX forwards are implied by covered interest parity in
  :mod:`amfe.markets_finance.fx_rates`.

The DGP is **not** an economic model and says nothing about real markets: it exists to exercise the
contracts, the estimators and the no-look-ahead machinery (docs/149).

Public API::

    dataset = synthetic_dataset(SyntheticMarketConfig(seed=20261116))
    dataset.equities["EQ01"].returns()
    dataset.factor_returns["MARKET"].shape
    dataset.returns_matrix("EQUITY")      # ({symbol: ReturnSeries}, matrix (T-1, N))
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from .contracts import (AssetClass, DataClass, MarketDataError, PriceSeries, Provenance,
                        ReturnSeries, canonical_sha256, ensure_finite)

__all__ = ["FACTOR_NAMES", "SyntheticMarketConfig", "SyntheticDataset", "synthetic_dataset",
           "business_days"]

FACTOR_NAMES: tuple[str, ...] = ("MARKET", "VALUE", "MOMENTUM", "QUALITY", "SIZE")

#: DGP parameters: (mean, vol) of the five factors per period.
_FACTOR_SPEC: dict[str, tuple[float, float]] = {
    "MARKET": (0.00040, 0.00950),
    "VALUE": (0.00010, 0.00450),
    "MOMENTUM": (0.00020, 0.00550),
    "QUALITY": (0.00008, 0.00350),
    "SIZE": (0.00005, 0.00450),
}

_EQUITY_BETA_MARKET = (0.70, 1.30)
_CRYPTO_BETA_MARKET = (1.20, 1.90)
_FX_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("EURUSD", "EUR", "USD"), ("USDJPY", "USD", "JPY"), ("USDGBP", "USD", "GBP"),
)
_CRYPTO_NAMES: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP")
_RATE_LEVELS: dict[str, float] = {"DGS3M": 0.040, "DGS2": 0.043, "DGS10": 0.042}


def business_days(start: str, count: int) -> tuple[str, ...]:
    """``count`` consecutive Monday-Friday ISO dates starting at ``start`` (no holiday calendar)."""
    if count < 1:
        raise MarketDataError(f"count must be positive, got {count}")
    current = date.fromisoformat(start)
    if current.weekday() > 4:  # weekend start: roll forward to Monday, deterministic
        current += timedelta(days=7 - current.weekday())
    days: list[str] = []
    while len(days) < count:
        if current.weekday() <= 4:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(days)


@dataclass(frozen=True)
class SyntheticMarketConfig:
    """Configuration of the synthetic market; validated so a typo never becomes a silent default."""

    seed: int = 20261116
    n_equities: int = 12
    n_crypto: int = 4
    n_periods: int = 320
    start: str = "2022-01-03"
    characteristic_phi: float = 0.98
    characteristic_vol: float = 0.30
    crypto_vol_scale: float = 2.60
    fx_noise_vol: float = 0.0030

    def __post_init__(self) -> None:
        if self.seed < 0:
            raise MarketDataError(f"seed must be non-negative, got {self.seed}")
        if self.n_equities < 6:
            raise MarketDataError("n_equities must be >= 6 (tercile sorts need six assets)")
        if self.n_crypto < 1:
            raise MarketDataError("n_crypto must be >= 1")
        if self.n_periods < 80:
            raise MarketDataError("n_periods must be >= 80 (estimation windows need history)")
        if not 0.0 < self.characteristic_phi < 1.0:
            raise MarketDataError(f"characteristic_phi must be in (0, 1), got {self.characteristic_phi}")
        if self.characteristic_vol <= 0.0 or self.fx_noise_vol < 0.0:
            raise MarketDataError("characteristic/fx volatility must be non-negative")
        date.fromisoformat(self.start)
        if len(_CRYPTO_NAMES) < self.n_crypto:
            raise MarketDataError(f"at most {len(_CRYPTO_NAMES)} synthetic crypto assets are defined")

    def to_dict(self) -> dict:
        return {"seed": self.seed, "n_equities": self.n_equities, "n_crypto": self.n_crypto,
                "n_periods": self.n_periods, "start": self.start,
                "characteristic_phi": self.characteristic_phi,
                "characteristic_vol": self.characteristic_vol,
                "crypto_vol_scale": self.crypto_vol_scale,
                "fx_noise_vol": self.fx_noise_vol}


@dataclass(frozen=True)
class SyntheticDataset:
    """A frozen, validated synthetic market snapshot with provenance and a content digest."""

    config: SyntheticMarketConfig
    dates: tuple[str, ...]
    equities: dict[str, PriceSeries]
    crypto: dict[str, PriceSeries]
    fx: dict[str, PriceSeries]
    rates: dict[str, np.ndarray]
    factor_returns: dict[str, np.ndarray]
    characteristics: dict[str, np.ndarray]

    # ---------------------------------------------------------------- accessors / aggregations
    def prices(self, asset_class: AssetClass) -> dict[str, PriceSeries]:
        if asset_class is AssetClass.EQUITY:
            return dict(self.equities)
        if asset_class is AssetClass.CRYPTO:
            return dict(self.crypto)
        if asset_class is AssetClass.FX:
            return dict(self.fx)
        raise MarketDataError(f"no price panel for {asset_class.value}")

    def symbols(self, asset_class: AssetClass) -> tuple[str, ...]:
        return tuple(sorted(self.prices(asset_class)))

    def returns_matrix(self, asset_class: AssetClass,
                       symbols: tuple[str, ...] | None = None) -> tuple[dict[str, ReturnSeries],
                                                                       np.ndarray]:
        """Aligned simple-return matrix ``(T-1, N)`` plus the validated return series."""
        panel = self.prices(asset_class)
        names = tuple(sorted(panel)) if symbols is None else tuple(symbols)
        if not names:
            raise MarketDataError("the symbol list is empty")
        series = {name: panel[name].returns() for name in names}
        matrix = np.column_stack([series[name].values for name in names])
        if not np.all(np.isfinite(matrix)):
            raise MarketDataError("the return matrix contains non-finite values")
        return series, matrix

    def characteristics_matrix(self, asset_class: AssetClass,
                               symbols: tuple[str, ...] | None = None) -> tuple[tuple[str, ...],
                                                                                np.ndarray]:
        """Characteristic tensor ``(T, N, K)`` aligned with ``dates`` (known at the close of T)."""
        names = self.symbols(asset_class) if symbols is None else tuple(symbols)
        tensor = np.stack([self.characteristics[name] for name in names], axis=1)
        return names, tensor

    def rate(self, series_id: str) -> np.ndarray:
        if series_id not in self.rates:
            raise MarketDataError(f"unknown rate series {series_id!r}; available: "
                                  f"{sorted(self.rates)}")
        return np.asarray(self.rates[series_id], dtype=float)

    def to_metadata(self) -> dict:
        return {"schema": "AMFE-FINANCE-SYNTHETIC/1.0.0", "config": self.config.to_dict(),
                "observations": len(self.dates), "first_date": self.dates[0],
                "last_date": self.dates[-1],
                "equities": sorted(self.equities), "crypto": sorted(self.crypto),
                "fx": sorted(self.fx), "rates": sorted(self.rates),
                "factors": list(self.factor_returns),
                "digest": canonical_sha256(
                    {name: np.asarray(values).round(12).tolist()
                     for name, values in self.factor_returns.items()})}

    @property
    def sha256(self) -> str:
        """Content digest over prices, factors and characteristics (deterministic)."""
        payload = {"config": self.config.to_dict(),
                   "prices": {name: np.asarray(series.prices).round(12).tolist()
                              for name, series in sorted(self.prices(AssetClass.EQUITY).items())},
                   "factors": {name: np.asarray(values).round(12).tolist()
                               for name, values in sorted(self.factor_returns.items())}}
        return canonical_sha256(payload)


def synthetic_dataset(config: SyntheticMarketConfig | None = None) -> SyntheticDataset:
    """Build the synthetic market; the same ``config`` always yields byte-identical arrays."""
    cfg = SyntheticMarketConfig() if config is None else config
    rng = np.random.default_rng(cfg.seed)
    dates = business_days(cfg.start, cfg.n_periods)
    periods = cfg.n_periods - 1  # number of returns

    factor_returns = {name: rng.normal(mean, vol, size=periods)
                      for name, (mean, vol) in _FACTOR_SPEC.items()}
    factors = np.column_stack([factor_returns[name] for name in FACTOR_NAMES])

    def characteristic_block(asset_count: int) -> np.ndarray:
        out = np.zeros((cfg.n_periods, asset_count, len(FACTOR_NAMES)), dtype=float)
        for index in range(asset_count):
            for k in range(len(FACTOR_NAMES)):
                noise = rng.normal(0.0, cfg.characteristic_vol, size=cfg.n_periods)
                level = 0.0
                for t in range(cfg.n_periods):
                    level = cfg.characteristic_phi * level + noise[t]
                    out[t, index, k] = level
        # standardise each characteristic cross-section to mean 0 / sd 1 (per date)
        for t in range(cfg.n_periods):
            for k in range(len(FACTOR_NAMES)):
                column = out[t, :, k]
                spread = float(np.std(column))
                out[t, :, k] = (column - float(np.mean(column))) / (spread if spread else 1.0)
        return out

    def equity_block(asset_class: AssetClass, count: int) -> tuple[dict[str, PriceSeries],
                                                                  dict[str, np.ndarray]]:
        chars = characteristic_block(count)
        prices_out: dict[str, PriceSeries] = {}
        chars_out: dict[str, np.ndarray] = {}
        for index in range(count):
            name = (f"EQ{index + 1:02d}" if asset_class is AssetClass.EQUITY
                    else _CRYPTO_NAMES[index])
            if asset_class is AssetClass.EQUITY:
                beta_market = rng.uniform(*_EQUITY_BETA_MARKET)
                idio_vol = rng.uniform(0.006, 0.014)
                alpha = rng.normal(0.0, 0.00005)
                start_price = float(rng.uniform(50.0, 250.0))
            else:
                beta_market = rng.uniform(*_CRYPTO_BETA_MARKET)
                idio_vol = rng.uniform(0.012, 0.020) * cfg.crypto_vol_scale
                alpha = rng.normal(0.0, 0.00020)
                start_price = float(rng.uniform(100.0, 40000.0))
            betas = np.array([beta_market,
                              rng.normal(0.0, 0.35), rng.normal(0.0, 0.30),
                              rng.normal(0.0, 0.25), rng.normal(0.0, 0.30)])
            idio = rng.normal(0.0, idio_vol * (1.0 if asset_class is AssetClass.EQUITY
                                               else cfg.crypto_vol_scale), size=periods)
            returns = alpha + factors @ betas + idio
            prices = start_price * np.cumprod(1.0 + returns)
            prices = np.concatenate([[start_price], prices])
            prices_out[name] = PriceSeries(
                name=name, asset_class=asset_class, dates=dates,
                prices=prices, currency="USD",
                provenance=_provenance(cfg, asset_class, name, prices))
            chars_out[name] = chars[:, index, :]
        return prices_out, chars_out

    equities, equity_chars = equity_block(AssetClass.EQUITY, cfg.n_equities)
    crypto, crypto_chars = equity_block(AssetClass.CRYPTO, cfg.n_crypto)
    characteristics = {**equity_chars, **crypto_chars}

    rates: dict[str, np.ndarray] = {}
    for series_id, level in _RATE_LEVELS.items():
        values = np.empty(cfg.n_periods, dtype=float)
        current = level
        for t in range(cfg.n_periods):
            current = 0.995 * current + 0.005 * level + rng.normal(0.0, 0.0006)
            values[t] = float(np.clip(current, 0.001, 0.15))
        rates[series_id] = values

    fx: dict[str, PriceSeries] = {}
    for pair, base_ccy, quote_ccy in _FX_PAIRS:
        # reduced-form FX: a fraction of the rate differential plus noise (no pricing model);
        # the differential known at the start of each return period drives that period's return
        differential = (rates["DGS3M"] - rates["DGS10"])[:-1] / 252.0
        returns = differential + rng.normal(0.0, cfg.fx_noise_vol, size=periods)
        start_price = {"EURUSD": 1.10, "USDJPY": 130.0, "USDGBP": 1.25}[pair]
        prices = start_price * np.cumprod(1.0 + returns)
        prices = np.concatenate([[start_price], prices])
        fx[pair] = PriceSeries(name=pair, asset_class=AssetClass.FX, dates=dates,
                               prices=prices, currency=quote_ccy,
                               provenance=_provenance(cfg, AssetClass.FX, pair, prices))

    return SyntheticDataset(config=cfg, dates=dates, equities=equities, crypto=crypto, fx=fx,
                            rates=rates, factor_returns=factor_returns,
                            characteristics=characteristics)


def _provenance(cfg: SyntheticMarketConfig, asset_class: AssetClass, name: str,
                prices: np.ndarray) -> Provenance:
    digest = canonical_sha256({"seed": cfg.seed, "asset_class": asset_class.value, "name": name,
                               "prices": np.asarray(prices).round(12).tolist()})
    return Provenance(source_id="SYNTHETIC_MARKET", dataset_id=f"synthetic/{cfg.seed}/{name}",
                      retrieved_at="2026-11-16", licence="internal synthetic data (no vendor terms)",
                      sha256=digest, data_class=DataClass.SYNTHETIC,
                      pit_note="known DGP; no vintage revisions - usable for method tests only")
