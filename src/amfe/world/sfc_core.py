"""Stock-flow consistent (SFC) matrix core (P2): balances, period step and cross-border netting.

Scope
-----
A static accounting core for the financial layer of docs/05 (multi-layer world model), docs/38
I05/I07 (cross-border flows net out; saving, capital account and revaluation reconcile with sector
balances) and docs/53 (stock identities).  It is a bookkeeping contract, not a behavioural model:
no prices, no interest rates, no portfolio choice.

Representation
--------------
A frame is a square matrix per phase over *institutional accounts*, each account belonging to a
country or to ``"ROW"`` (rest of world):

    M[holder][issuer] = amount of financial claims held by ``holder`` on ``issuer``.

The same matrix is simultaneously the asset view (rows: assets of the holder) and the liability
view (columns: liabilities of the issuer), so exactly one entry per claim exists and no claim is
double-counted (docs/38 I05/I08).  The four phases are

    opening  +  transactions  +  revaluation  =  closing        (elementwise, checked)

where ``transactions`` is the net acquisition of claims during the period and ``revaluation`` is the
signed holding gain (it may be negative).  Net worth of an account is ``row sum - column sum``.

Invariants (all fail-closed; a violation raises :class:`SFCError`)
------------------------------------------------------------------
* closing = opening + transactions + revaluation (exact elementwise identity);
* row totals equal column totals in every phase (every claim has one holder and one issuer), i.e.
  world assets equal world liabilities and total net worth is zero;
* per-account net worth reconciles: ``NW_closing = NW_opening + net_lending + revaluation_gain``;
* cross-border positions net out: the sum over countries of each country's net external position is
  zero in every phase, and a bilateral position is antisymmetric by construction.

Public API
----------
``SFCFrame``   validated four-phase frame with invariants, cross-border positions and a period step.
``SFCError``   invalid frame or a violated accounting invariant.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

TOLERANCE = 1e-9
DEFAULT_UNITS = "USD_mn"
PHASES = ("opening", "transactions", "revaluation", "closing")
REST_OF_WORLD = "ROW"


class SFCError(ValueError):
    """Invalid SFC data or a violated accounting invariant."""


# --------------------------------------------------------------------------------- input coercion
def _labels(values: Iterable[object], name: str) -> tuple[str, ...]:
    labels = tuple(str(value).strip() for value in values)
    if not labels:
        raise SFCError(f"{name} must not be empty")
    if any(not label for label in labels):
        raise SFCError(f"{name} must not contain empty labels")
    if len(set(labels)) != len(labels):
        raise SFCError(f"{name} must be unique")
    return labels


def _country_labels(values: Iterable[object], name: str) -> tuple[str, ...]:
    """Country labels per account: duplicates are expected, emptiness is not."""
    labels = tuple(str(value).strip() for value in values)
    if not labels:
        raise SFCError(f"{name} must not be empty")
    if any(not label for label in labels):
        raise SFCError(f"{name} must not contain empty labels")
    return labels


def _square(rows: Sequence[Sequence[float]], name: str, *, size: int | None = None,
            non_negative: bool = True, tolerance: float = TOLERANCE) -> list[list[float]]:
    try:
        matrix = [[float(value) for value in row] for row in rows]
    except (TypeError, ValueError) as exc:
        raise SFCError(f"{name} must be a matrix of numbers") from exc
    if not matrix:
        raise SFCError(f"{name} must not be empty")
    rows_count = len(matrix)
    for index, row in enumerate(matrix):
        if len(row) != rows_count:
            raise SFCError(f"{name} must be square: row {index} has {len(row)} entries")
    if size is not None and rows_count != int(size):
        raise SFCError(f"{name} must be {int(size)} x {int(size)}, got {rows_count} x {rows_count}")
    for row in matrix:
        for value in row:
            if not math.isfinite(value):
                raise SFCError(f"{name} contains non-finite values")
            if non_negative and value < -tolerance:
                raise SFCError(
                    f"{name} must be non-negative: claims, liabilities and transactions cannot "
                    f"be negative (use the revaluation matrix for signed holding gains), got {value!r}")
    return matrix


def _row_sums(matrix: Sequence[Sequence[float]]) -> list[float]:
    return [math.fsum(row) for row in matrix]


def _column_sums(matrix: Sequence[Sequence[float]]) -> list[float]:
    size = len(matrix)
    return [math.fsum(matrix[row][column] for row in range(size)) for column in range(size)]


@dataclass(frozen=True)
class SFCFrame:
    """Four-phase SFC frame: opening balances, transactions, revaluation, closing balances."""

    accounts: tuple[str, ...]
    account_countries: tuple[str, ...]
    opening: tuple[tuple[float, ...], ...]
    transactions: tuple[tuple[float, ...], ...]
    revaluation: tuple[tuple[float, ...], ...]
    closing: tuple[tuple[float, ...], ...]
    units: str = DEFAULT_UNITS
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        accounts = _labels(self.accounts, "accounts")
        size = len(accounts)
        countries = _country_labels(self.account_countries, "account_countries")
        if len(countries) != size:
            raise SFCError(
                f"account_countries must have one entry per account ({size}), got {len(countries)}")
        tolerance = float(self.tolerance)
        if not math.isfinite(tolerance) or tolerance <= 0.0:
            raise SFCError("tolerance must be a positive finite number")
        opening = _square(self.opening, "opening", size=size, tolerance=tolerance)
        transactions = _square(self.transactions, "transactions", size=size, tolerance=tolerance)
        revaluation = _square(self.revaluation, "revaluation", size=size, non_negative=False)
        closing = _square(self.closing, "closing", size=size, tolerance=tolerance)
        units = str(self.units).strip()
        if not units:
            raise SFCError("units must be a non-empty string")
        object.__setattr__(self, "accounts", accounts)
        object.__setattr__(self, "account_countries", countries)
        object.__setattr__(self, "opening", tuple(tuple(row) for row in opening))
        object.__setattr__(self, "transactions", tuple(tuple(row) for row in transactions))
        object.__setattr__(self, "revaluation", tuple(tuple(row) for row in revaluation))
        object.__setattr__(self, "closing", tuple(tuple(row) for row in closing))
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "tolerance", tolerance)
        self._validate_invariants()

    @classmethod
    def from_flows(cls, *, accounts: Sequence[object], opening: Sequence[Sequence[float]],
                   transactions: Sequence[Sequence[float]],
                   revaluation: Sequence[Sequence[float]],
                   account_countries: Sequence[object] | None = None,
                   units: str = DEFAULT_UNITS, tolerance: float = TOLERANCE) -> "SFCFrame":
        """Build a frame from the three observed phases; closing is derived, never guessed."""
        labels = _labels(accounts, "accounts")
        size = len(labels)
        opening_m = _square(opening, "opening", size=size, tolerance=tolerance)
        transactions_m = _square(transactions, "transactions", size=size, tolerance=tolerance)
        revaluation_m = _square(revaluation, "revaluation", size=size, non_negative=False)
        closing_m = [[opening_m[row][column] + transactions_m[row][column]
                      + revaluation_m[row][column] for column in range(size)] for row in range(size)]
        countries = tuple(labels if account_countries is None else account_countries)
        return cls(accounts=labels, account_countries=countries, opening=opening_m,
                   transactions=transactions_m, revaluation=revaluation_m,
                   closing=closing_m, units=units, tolerance=tolerance)

    # -- basics -------------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self.accounts)

    @property
    def countries(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.account_countries)))

    def _matrix(self, phase: str) -> list[list[float]]:
        if phase not in PHASES:
            raise SFCError(f"phase must be one of {PHASES}, got {phase!r}")
        return [list(row) for row in getattr(self, phase)]

    def matrix(self, phase: str = "closing") -> list[list[float]]:
        return self._matrix(phase)

    def _scale(self) -> float:
        values = [value for phase in PHASES for row in self._matrix(phase) for value in row]
        return max(1.0, max(abs(value) for value in values))

    # -- invariants ---------------------------------------------------------------------
    def _validate_invariants(self) -> None:
        tolerance = self.tolerance * self._scale()
        identity = self.closing_identity_residual()
        if identity > tolerance:
            raise SFCError(
                "closing != opening + transactions + revaluation: max |residual| = "
                f"{identity:.6e} > tolerance {tolerance:.3e}")
        for phase in PHASES:
            residual = self.row_column_residual(phase)
            if residual > tolerance:
                raise SFCError(
                    f"row totals != column totals in phase {phase!r}: {residual:.6e} > "
                    f"tolerance {tolerance:.3e}; every claim needs exactly one holder and one issuer")
            net_worth = abs(self.total_net_worth(phase))
            if net_worth > tolerance:
                raise SFCError(
                    f"world net worth is not zero in phase {phase!r}: {net_worth:.6e} > "
                    f"tolerance {tolerance:.3e}")
            external = abs(self.external_residual(phase))
            if external > tolerance:
                raise SFCError(
                    f"cross-border positions do not net out in phase {phase!r}: "
                    f"{external:.6e} > tolerance {tolerance:.3e}")
        reconciliation = self.net_worth_residual()
        if reconciliation > tolerance:
            raise SFCError(
                "net worth does not reconcile: NW_closing - NW_opening - net_lending - "
                f"revaluation_gain = {reconciliation:.6e} > tolerance {tolerance:.3e}")

    def closing_identity_residual(self) -> float:
        """``max |closing - opening - transactions - revaluation|``."""
        size = self.size
        worst = 0.0
        for row in range(size):
            for column in range(size):
                expected = (self.opening[row][column] + self.transactions[row][column]
                            + self.revaluation[row][column])
                worst = max(worst, abs(self.closing[row][column] - expected))
        return worst

    def row_totals(self, phase: str) -> list[float]:
        """Assets (claims held) of each account."""
        return _row_sums(self._matrix(phase))

    def column_totals(self, phase: str) -> list[float]:
        """Liabilities (claims issued) of each account."""
        return _column_sums(self._matrix(phase))

    def row_column_residual(self, phase: str) -> float:
        """``|sum(row totals) - sum(column totals)|``: zero because both are the grand total."""
        return abs(math.fsum(self.row_totals(phase)) - math.fsum(self.column_totals(phase)))

    def assets_total(self, phase: str) -> float:
        return math.fsum(self.row_totals(phase))

    def liabilities_total(self, phase: str) -> float:
        return math.fsum(self.column_totals(phase))

    def net_worths(self, phase: str) -> dict[str, float]:
        """``assets - liabilities`` per account (a closed system's total is zero)."""
        assets = self.row_totals(phase)
        liabilities = self.column_totals(phase)
        return {account: assets[index] - liabilities[index]
                for index, account in enumerate(self.accounts)}

    def total_net_worth(self, phase: str) -> float:
        return math.fsum(self.net_worths(phase).values())

    def net_worth_residual(self) -> float:
        """Max per-account residual of ``NW_closing = NW_opening + net_lending + revaluation gain``."""
        opening = self.net_worths("opening")
        closing = self.net_worths("closing")
        transactions = self.row_totals("transactions")
        payments = self.column_totals("transactions")
        gains = self.row_totals("revaluation")
        losses = self.column_totals("revaluation")
        worst = 0.0
        for index, account in enumerate(self.accounts):
            expected = (opening[account] + transactions[index] - payments[index]
                        + gains[index] - losses[index])
            worst = max(worst, abs(closing[account] - expected))
        return worst

    # -- cross-border -------------------------------------------------------------------
    def country_net_worths(self, phase: str) -> dict[str, float]:
        net_worths = self.net_worths(phase)
        totals: dict[str, float] = {}
        for index, account in enumerate(self.accounts):
            country = self.account_countries[index]
            totals[country] = totals.get(country, 0.0) + net_worths[account]
        return totals

    def country_balance_sheet(self, phase: str = "closing") -> dict[str, dict]:
        """Per-country assets, liabilities, net worth and net external position."""
        assets = self.row_totals(phase)
        liabilities = self.column_totals(phase)
        external = self.external_positions(phase)
        book: dict[str, dict] = {}
        for index, account in enumerate(self.accounts):
            country = self.account_countries[index]
            entry = book.setdefault(country, {"assets": 0.0, "liabilities": 0.0,
                                              "net_worth": 0.0,
                                              "net_external_position": external.get(country, 0.0)})
            entry["assets"] += assets[index]
            entry["liabilities"] += liabilities[index]
            entry["net_worth"] += assets[index] - liabilities[index]
        return book

    def external_positions(self, phase: str) -> dict[str, float]:
        """Net claims of each country on the rest of the world (assets abroad - liabilities to it)."""
        matrix = self._matrix(phase)
        positions: dict[str, float] = {country: 0.0 for country in self.countries}
        for row, holder in enumerate(self.accounts):
            holder_country = self.account_countries[row]
            for column, issuer in enumerate(self.accounts):
                issuer_country = self.account_countries[column]
                if holder_country == issuer_country:
                    continue
                value = matrix[row][column]
                positions[holder_country] += value
                positions[issuer_country] -= value
        return positions

    def external_residual(self, phase: str) -> float:
        """``|sum_country net external position|``: must be zero (world is closed)."""
        return abs(math.fsum(self.external_positions(phase).values()))

    def bilateral_position(self, phase: str, country_a: str, country_b: str) -> float:
        """Net claims of ``country_a`` on ``country_b``: assets abroad minus liabilities to it."""
        matrix = self._matrix(phase)
        net = 0.0
        for row, holder in enumerate(self.accounts):
            holder_country = self.account_countries[row]
            for column, issuer in enumerate(self.accounts):
                issuer_country = self.account_countries[column]
                if holder_country == country_a and issuer_country == country_b:
                    net += matrix[row][column]
                elif holder_country == country_b and issuer_country == country_a:
                    net -= matrix[row][column]
        return net

    # -- period step --------------------------------------------------------------------
    def step_period(self, transactions: Sequence[Sequence[float]],
                    revaluation: Sequence[Sequence[float]]) -> "SFCFrame":
        """Advance one period: the next opening equals this closing, closing is derived."""
        moves = _square(transactions, "transactions", size=self.size, tolerance=self.tolerance)
        revaluation_m = _square(revaluation, "revaluation", size=self.size, non_negative=False)
        opening = self._matrix("closing")
        closing = [[opening[row][column] + moves[row][column] + revaluation_m[row][column]
                    for column in range(self.size)] for row in range(self.size)]
        return SFCFrame(accounts=self.accounts, account_countries=self.account_countries,
                        opening=opening, transactions=moves, revaluation=revaluation_m,
                        closing=closing, units=self.units, tolerance=self.tolerance)

    # -- reporting ----------------------------------------------------------------------
    def consistency(self) -> dict:
        """All residuals and pass flags of the invariants (fail-closed construction guarantees pass)."""
        scale = self.tolerance * self._scale()
        row_column = {phase: self.row_column_residual(phase) for phase in PHASES}
        net_worth = {phase: abs(self.total_net_worth(phase)) for phase in PHASES}
        external = {phase: self.external_residual(phase) for phase in PHASES}
        closing_identity = self.closing_identity_residual()
        reconciliation = self.net_worth_residual()
        residuals = [closing_identity, reconciliation, *row_column.values(),
                     *net_worth.values(), *external.values()]
        largest = max(residuals)
        return {"closing_identity_residual": closing_identity,
                "row_column_residual": row_column,
                "world_net_worth_residual": net_worth,
                "external_position_residual": external,
                "net_worth_reconciliation_residual": reconciliation,
                "max_abs_residual": largest,
                "tolerance": scale,
                "pass": largest <= scale}

    def describe(self) -> dict:
        """Metadata-only summary: no raw matrix is returned."""
        countries = self.countries
        return {"units": self.units, "accounts": len(self.accounts), "countries": list(countries),
                "assets_total": {phase: round(self.assets_total(phase), 9) for phase in PHASES},
                "net_worth_total": {phase: round(self.total_net_worth(phase), 9)
                                    for phase in PHASES},
                "net_external_positions": {country: round(value, 9) for country, value
                                           in self.external_positions("closing").items()},
                "max_abs_residual": round(self.consistency()["max_abs_residual"], 12),
                "consistent": bool(self.consistency()["pass"])}


def uniform_frame(accounts: Sequence[object], *, opening: float = 0.0, units: str = DEFAULT_UNITS,
                  countries: Sequence[object] | None = None,
                  tolerance: float = TOLERANCE) -> SFCFrame:
    """Convenience constructor: a frame with no opening claims and a zero first period."""
    labels = _labels(accounts, "accounts")
    size = len(labels)
    zeros = [[0.0] * size for _ in range(size)]
    filled = [[float(opening)] * size for _ in range(size)]
    return SFCFrame.from_flows(accounts=labels, opening=filled if opening else zeros,
                               transactions=zeros, revaluation=zeros,
                               account_countries=countries, units=units, tolerance=tolerance)


__all__ = ["DEFAULT_UNITS", "PHASES", "REST_OF_WORLD", "SFCFrame", "SFCError", "TOLERANCE",
           "uniform_frame"]
