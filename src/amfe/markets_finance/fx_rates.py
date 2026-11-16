"""FX and rates block of the finance prototype (docs/149): CIP, carry, conversion.

The block is a bookkeeping layer, not a pricing model: it enforces the **covered interest parity**
identity, converts local returns into a base currency and exposes the carry/rate differential that
the factor model uses as an FX control.  Nothing here forecasts an exchange rate; the demo and the
backtest treat FX as a separate panel precisely because a reduced-form FX model would overstate
what the prototype can support.

Conventions (documented and tested):

* spot ``S`` is quoted as *quote currency per unit of base currency* (EURUSD = USD per EUR);
* ``r_base`` is the base-currency interest rate, ``r_quote`` the quote-currency rate; both are
  annualised simple rates with an ACT/360 day count (``day_count`` parameter);
* CIP forward ``F = S * (1 + r_quote * d / day_count) / (1 + r_base * d / day_count)``;
* the CIP residual is ``F / S - (1 + r_quote d / dc) / (1 + r_base d / dc)`` and is zero by
  construction for the forward implied above - nonzero on real quotes (basis, funding, credit);
* carry (unhedged expected return from the rate differential) is
  ``(r_quote - r_base) * d / day_count`` for the *base* currency investor.

Public API::

    curve = RateCurve("2026-11-16", (90, 730, 3650), (0.040, 0.043, 0.042))
    forward = cip_forward(1.10, 0.043, 0.040, 90)     # EURUSD, USD rate 4.3%, EUR rate 4.0%
    residual = cip_residual(1.10, forward, 0.043, 0.040, 90)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import DataClass, AssetClass, MarketDataError, ModelCard, ModelStatus, ensure_finite, log_returns

__all__ = ["RateCurve", "FXQuote", "cip_forward", "cip_residual", "carry_return",
           "unhedged_return", "hedged_return", "usd_return", "real_rate", "term_spread",
           "fx_cross", "FXBlock"]


@dataclass(frozen=True)
class RateCurve:
    """A small tenor curve with linear interpolation and flat extrapolation (simple rates)."""

    as_of: str
    tenors_days: tuple[int, ...]
    rates: tuple[float, ...]
    day_count: int = 360

    def __post_init__(self) -> None:
        if len(self.tenors_days) != len(self.rates):
            raise MarketDataError("tenors and rates must have the same length")
        if len(self.tenors_days) < 2:
            raise MarketDataError("a curve needs at least two tenors")
        tenors = tuple(int(item) for item in self.tenors_days)
        if any(item <= 0 for item in tenors):
            raise MarketDataError("tenors must be positive day counts")
        if any(current <= previous for previous, current in zip(tenors, tenors[1:])):
            raise MarketDataError("tenors must be strictly increasing")
        values = ensure_finite(np.asarray(self.rates, dtype=float), "rates")
        if np.any(values <= -1.0):
            raise MarketDataError("rates must be greater than -100%")
        if self.day_count not in (360, 365):
            raise MarketDataError(f"day_count must be 360 or 365, got {self.day_count}")
        object.__setattr__(self, "tenors_days", tenors)
        object.__setattr__(self, "rates", tuple(float(item) for item in values))

    def rate_at(self, days: int) -> float:
        """Linearly interpolated simple rate for ``days`` (flat outside the quoted tenors)."""
        if days <= 0:
            raise MarketDataError(f"days must be positive, got {days}")
        return float(np.interp(float(days), np.asarray(self.tenors_days, dtype=float),
                              np.asarray(self.rates, dtype=float)))

    def discount_factor(self, days: int) -> float:
        """Simple-interest discount factor ``1 / (1 + r * d / day_count)``."""
        rate = self.rate_at(days)
        return 1.0 / (1.0 + rate * days / self.day_count)

    def forward_rate(self, start_days: int, end_days: int) -> float:
        """Implied simple forward rate between two tenors (no arbitrage on the curve)."""
        if not 0 < start_days < end_days:
            raise MarketDataError(f"require 0 < start < end, got {start_days}/{end_days}")
        first = self.discount_factor(start_days)
        second = self.discount_factor(end_days)
        period = (end_days - start_days) / self.day_count
        return float((first / second - 1.0) / period)

    def to_dict(self) -> dict:
        return {"as_of": self.as_of, "tenors_days": list(self.tenors_days),
                "rates": list(self.rates), "day_count": self.day_count}


def cip_forward(spot: float, r_quote: float, r_base: float, days: int,
                day_count: int = 360) -> float:
    """Covered-interest-parity forward (quote per base) for a positive spot and day count."""
    spot = float(spot)
    if not np.isfinite(spot) or spot <= 0.0:
        raise MarketDataError(f"spot must be a positive finite number, got {spot}")
    if days <= 0:
        raise MarketDataError(f"days must be positive, got {days}")
    for name, rate in (("r_quote", r_quote), ("r_base", r_base)):
        if not np.isfinite(rate) or rate <= -1.0:
            raise MarketDataError(f"{name} must be finite and > -100%, got {rate}")
    period = float(days) / float(day_count)
    return spot * (1.0 + float(r_quote) * period) / (1.0 + float(r_base) * period)


def cip_residual(spot: float, forward: float, r_quote: float, r_base: float, days: int,
                 day_count: int = 360) -> float:
    """Relative CIP violation of a quoted forward (0 for the CIP-implied forward)."""
    implied = cip_forward(spot, r_quote, r_base, days, day_count=day_count)
    return float(forward / implied - 1.0)


def carry_return(r_quote: float, r_base: float, days: int, day_count: int = 360) -> float:
    """Unhedged carry for the base-currency holder (rate differential over the period)."""
    if days <= 0:
        raise MarketDataError(f"days must be positive, got {days}")
    return float((float(r_quote) - float(r_base)) * float(days) / float(day_count))


def unhedged_return(local_return: float, fx_return: float) -> float:
    """Return of a foreign asset for a base-currency investor: ``(1+r_local)(1+fx) - 1``."""
    for name, value in (("local_return", local_return), ("fx_return", fx_return)):
        if not np.isfinite(value):
            raise MarketDataError(f"{name} must be finite")
        if value <= -1.0:
            raise MarketDataError(f"{name} must be > -100%")
    return float((1.0 + float(local_return)) * (1.0 + float(fx_return)) - 1.0)


def hedged_return(local_return: float, carry: float) -> float:
    """Approximate hedged return: ``r_local + carry`` (first-order forward hedge, documented)."""
    if not np.isfinite(local_return) or not np.isfinite(carry):
        raise MarketDataError("local_return and carry must be finite")
    return float(local_return) + float(carry)


def usd_return(local_returns: np.ndarray, pair_returns: np.ndarray,
               pair_is_usd_per_base: bool = True) -> np.ndarray:
    """Convert a local-return panel into USD returns using a currency-pair panel."""
    local = ensure_finite(local_returns, "local_returns")
    fx = ensure_finite(pair_returns, "pair_returns")
    if local.shape != fx.shape:
        raise MarketDataError(f"local {local.shape} and FX {fx.shape} panels are misaligned")
    fx_for_conversion = fx if pair_is_usd_per_base else -fx / (1.0 + fx)
    return (1.0 + local) * (1.0 + fx_for_conversion) - 1.0


def real_rate(nominal: float, inflation: float) -> float:
    """Fisher relation ``(1 + nominal) / (1 + inflation) - 1``."""
    if inflation <= -1.0:
        raise MarketDataError(f"inflation must be > -100%, got {inflation}")
    return float((1.0 + float(nominal)) / (1.0 + float(inflation)) - 1.0)


def term_spread(long_rate: float, short_rate: float) -> float:
    """Term spread in rate points (long minus short); sign carries the usual curve interpretation."""
    return float(long_rate) - float(short_rate)


def fx_cross(base_usd: float, quote_usd: float) -> float:
    """Cross rate from two USD quotes: ``base_usd / quote_usd`` (USD per base / USD per quote)."""
    if base_usd <= 0.0 or quote_usd <= 0.0:
        raise MarketDataError("USD quotes must be positive")
    return float(base_usd) / float(quote_usd)


@dataclass(frozen=True)
class FXQuote:
    """A single spot quote with its two rates; forwards and carry follow from the CIP identity."""

    pair: str
    spot: float
    r_quote: float
    r_base: float
    as_of: str = "2026-11-16"
    day_count: int = 360

    def __post_init__(self) -> None:
        if not self.pair or len(self.pair) != 6:
            raise MarketDataError(f"pair must be a six-letter code, got {self.pair!r}")
        if not np.isfinite(self.spot) or self.spot <= 0.0:
            raise MarketDataError(f"spot must be positive and finite, got {self.spot}")
        if not np.isfinite(self.r_quote) or not np.isfinite(self.r_base):
            raise MarketDataError("rates must be finite")
        if self.r_quote <= -1.0 or self.r_base <= -1.0:
            raise MarketDataError("rates must be greater than -100%")
        if self.day_count not in (360, 365):
            raise MarketDataError(f"day_count must be 360 or 365, got {self.day_count}")

    def forward(self, days: int) -> float:
        return cip_forward(self.spot, self.r_quote, self.r_base, days,
                           day_count=self.day_count)

    def residual(self, days: int, forward: float | None = None) -> float:
        quoted = self.forward(days) if forward is None else float(forward)
        return cip_residual(self.spot, quoted, self.r_quote, self.r_base, days,
                            day_count=self.day_count)

    def carry(self, days: int) -> float:
        return carry_return(self.r_quote, self.r_base, days, day_count=self.day_count)

    def to_dict(self) -> dict:
        return {"pair": self.pair, "spot": self.spot, "r_quote": self.r_quote,
                "r_base": self.r_base, "as_of": self.as_of, "day_count": self.day_count,
                "forward_90d": self.forward(90), "carry_90d": self.carry(90)}


class FXBlock:
    """Convenience wrapper: a pair panel, its quotes and the CIP/carry bookkeeping."""

    def __init__(self, pair: str, spot: np.ndarray, r_quote: np.ndarray, r_base: np.ndarray,
                 dates: tuple[str, ...], day_count: int = 360) -> None:
        if not pair:
            raise MarketDataError("pair is empty")
        self.pair = pair
        spots = ensure_finite(spot, "spot")
        if spots.size != len(dates):
            raise MarketDataError("spot and dates are misaligned")
        if np.any(spots <= 0.0):
            raise MarketDataError("spot must be positive")
        self.spot = spots
        self.dates = tuple(dates)
        self.r_quote = ensure_finite(r_quote, "r_quote")
        self.r_base = ensure_finite(r_base, "r_base")
        if not (self.r_quote.size == self.r_base.size == spots.size):
            raise MarketDataError("rates and spot must have the same length")
        self.day_count = day_count

    def log_returns(self) -> np.ndarray:
        return log_returns(self.spot)

    def carry(self, days: int) -> float:
        """Average carry over the sample (rate differential of the last observation)."""
        return carry_return(float(self.r_quote[-1]), float(self.r_base[-1]), days,
                            day_count=self.day_count)

    def cip_residuals(self, days: int = 90) -> np.ndarray:
        """Per-date CIP residuals of the *implied* forward (zero by construction, a self-check)."""
        forwards = np.array([cip_forward(s, rq, rb, days, day_count=self.day_count)
                             for s, rq, rb in zip(self.spot, self.r_quote, self.r_base)])
        return np.array([cip_residual(s, f, rq, rb, days, day_count=self.day_count)
                         for s, f, rq, rb in zip(self.spot, forwards,
                                                 self.r_quote, self.r_base)])

    def model_card(self) -> ModelCard:
        return ModelCard(
            model_id="AMFE-FINANCE-FX-RATES", version="0.1.0",
            kind="FX/rates bookkeeping: CIP forward, carry, currency conversion",
            inputs=("spot panel", "base/quote rates", "day count"),
            outputs=("CIP forward/residual", "carry", "USD returns", "rate diagnostics"),
            data_classes=(DataClass.SYNTHETIC, DataClass.LOCAL_CSV),
            limitations=("no FX forecasting: the block is an identity, not a model",
                         "CIP is assumed: real markets show basis/funding deviations",
                         "ACT/360 simple rates only; no day-count calendar or holidays",
                         "no cross-currency basis, collateral or credit adjustment"),
            tests=("tests/test_markets_finance.py::FXRatesTests",),
            status=ModelStatus.RESEARCH_ONLY, deterministic=True)
