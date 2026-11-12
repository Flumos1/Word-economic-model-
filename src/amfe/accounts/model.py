"""Typed sector accounts for AMFE-17 stage 2: types, chart of accounts, postings.

This module is deliberately *pure data*: it defines

* the typed sectors (households, firms, banks, government) and account
  dimensions (current, capital, real, financial);
* signed financial positions (asset ``+``, liability ``-``);
* the chart of accounts with the economic roles of every account;
* atomic double-entry postings and transactions;
* immutable balance sheets, snapshots and period reports.

The period lifecycle and the invariant enforcement live in
:mod:`amfe.accounts.accounts` and :mod:`amfe.accounts.invariants`.

Conventions (see docs/77 for the full description)
--------------------------------------------------
* Amounts are exact :class:`decimal.Decimal` values in one nominal unit.
  ``float`` inputs are accepted only through their shortest string form;
  ``bool``, ``NaN`` and ``Infinity`` are rejected.
* A *financial position* is a stock: assets are positive, liabilities are
  negative.  The sum of all financial positions of an instrument over all
  sectors is zero (the mirroring invariant).
* A *current* posting is a saving flow: receipts ``+``, expenditures ``-``.
* A *capital* posting is a capital transfer: receipts ``+``, payments ``-``.
* A *real* posting is a transaction change of a real asset: acquisition ``+``,
  disposal ``-``.
* Every transaction is closed per sector:
  ``current + capital == real + financial``.
  That single rule produces the double-entry journal and, together with the
  stock rules, the period identities checked by :mod:`amfe.accounts.invariants`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AccountingError(ValueError):
    """Base class for every accounting violation in this package."""

    code = "accounting_error"

    def __init__(self, message: str, *, code: str | None = None,
                 details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details = dict(details or {})


class TransactionRejected(AccountingError):
    """An atomic transaction was rejected and nothing was posted."""

    code = "transaction_rejected"


class InvariantViolation(AccountingError):
    """A stock or period invariant does not hold."""

    code = "invariant_violation"


class PeriodStateError(AccountingError):
    """The requested operation is illegal for the current period state."""

    code = "period_state"


# ---------------------------------------------------------------------------
# Small typed helpers
# ---------------------------------------------------------------------------
def to_decimal(value: object, *, what: str = "amount") -> Decimal:
    """Convert a user-supplied number to an exact finite :class:`Decimal`.

    ``bool`` is rejected even though it is an ``int`` subclass; non-finite
    values are rejected as well.  ``float`` is converted via ``str`` so that
    the literal decimal value typed by the caller is preserved.
    """
    if isinstance(value, bool):
        raise AccountingError(f"invalid {what}: bool is not a number")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise AccountingError(f"invalid {what}: empty string")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - message path
            raise AccountingError(f"invalid {what}: {value!r}") from exc
    else:
        raise AccountingError(
            f"invalid {what}: expected Decimal/int/str, got {type(value).__name__}")
    if not number.is_finite():
        raise AccountingError(f"invalid {what}: non-finite value {number}")
    return number


def code_string(value: object, *, what: str = "code") -> str:
    """Normalise an enum member or a plain string to a non-empty code string."""
    if type(value) is str:
        text = value.strip()
        if not text:
            raise AccountingError(f"invalid {what}: {value!r}")
        return text
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or not value.strip():
        raise AccountingError(f"invalid {what}: {value!r}")
    return value.strip()


def _role_set(values: Iterable[object], *, what: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or isinstance(values, Enum):
        raise AccountingError(f"invalid {what}: expected a collection of sectors")
    try:
        items = frozenset(code_string(item, what=what) for item in values)
    except TypeError as exc:
        raise AccountingError(f"invalid {what}") from exc
    return items


# ---------------------------------------------------------------------------
# Typed codes
# ---------------------------------------------------------------------------
class Sector(str, Enum):
    """Resident institutional sectors of the one-country accounting kernel."""

    HOUSEHOLD = "household"
    FIRM = "firm"
    BANK = "bank"
    GOVERNMENT = "government"


class Dimension(str, Enum):
    """Account dimension of a posting."""

    CURRENT = "current"
    CAPITAL = "capital"
    REAL = "real"
    FINANCIAL = "financial"


class CurrentFlow(str, Enum):
    """Current-account flows (saving flows)."""

    OUTPUT = "output"
    WAGES = "wages"
    DIRECT_TAX = "direct_tax"
    TRANSFER = "transfer"
    INTEREST = "interest"
    HOUSEHOLD_CONSUMPTION = "household_consumption"
    GOVERNMENT_CONSUMPTION = "government_consumption"
    DEPRECIATION = "depreciation"


class CapitalFlow(str, Enum):
    """Capital-account flows (capital transfers only in this version)."""

    CAPITAL_TRANSFER = "capital_transfer"


class RealAsset(str, Enum):
    """Real (non-financial) assets held inside the kernel."""

    INVENTORY = "inventory"
    FIXED_CAPITAL = "fixed_capital"


class FinancialInstrument(str, Enum):
    """Signed financial instruments (asset ``+``, liability ``-``)."""

    DEPOSIT = "deposit"
    LOAN = "loan"
    BOND = "bond"
    RECEIVABLE = "receivable"


class TransactionKind(str, Enum):
    """Standard economic event kinds accepted by the default chart."""

    LOAN = "loan"
    LOAN_REPAYMENT = "loan_repayment"
    PRODUCTION = "production"
    WAGES = "wages"
    DIRECT_TAX = "direct_tax"
    TRANSFER = "transfer"
    INTEREST = "interest"
    HOUSEHOLD_PURCHASE = "household_purchase"
    GOVERNMENT_PURCHASE = "government_purchase"
    BOND_ISSUE = "bond_issue"
    BOND_REPAYMENT = "bond_repayment"
    CAPITAL_TRANSFER = "capital_transfer"
    ACCRUED_WAGES = "accrued_wages"
    SETTLE_ACCRUED_WAGES = "settle_accrued_wages"
    FIXED_INVESTMENT = "fixed_investment"
    DEPRECIATION = "depreciation"


# ---------------------------------------------------------------------------
# Postings and transactions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Posting:
    """One signed entry of an atomic transaction.

    Use the ``current``/``capital``/``real``/``financial`` constructors instead
    of the raw dataclass where possible; they enforce the code type.
    """

    sector: object
    dimension: object
    code: object
    amount: object

    def __post_init__(self) -> None:
        sector = self.sector
        if type(sector) is not str:
            if isinstance(sector, Enum):
                sector = sector.value
            if type(sector) is not str:
                raise AccountingError(f"invalid sector: {sector!r}")
        sector = sector.strip()
        if not sector:
            raise AccountingError("invalid sector: empty")
        object.__setattr__(self, "sector", sector)
        dimension = self.dimension
        if not isinstance(dimension, Dimension):
            try:
                dimension = Dimension(code_string(dimension, what="dimension"))
            except ValueError as exc:
                raise AccountingError(f"unknown dimension: {dimension!r}") from exc
        object.__setattr__(self, "dimension", dimension)
        code = self.code
        if type(code) is not str:
            if isinstance(code, Enum):
                code = code.value
            if type(code) is not str:
                raise AccountingError(f"invalid account code: {code!r}")
        code = code.strip()
        if not code:
            raise AccountingError("invalid account code: empty")
        object.__setattr__(self, "code", code)
        amount = self.amount
        if type(amount) is not Decimal:
            amount = to_decimal(amount)
        object.__setattr__(self, "amount", amount)

    @classmethod
    def current(cls, sector: object, flow: object, amount: object) -> "Posting":
        return cls(sector, Dimension.CURRENT, flow, amount)

    @classmethod
    def capital(cls, sector: object, flow: object, amount: object) -> "Posting":
        return cls(sector, Dimension.CAPITAL, flow, amount)

    @classmethod
    def real(cls, sector: object, asset: object, amount: object) -> "Posting":
        return cls(sector, Dimension.REAL, asset, amount)

    @classmethod
    def financial(cls, sector: object, instrument: object, amount: object) -> "Posting":
        return cls(sector, Dimension.FINANCIAL, instrument, amount)

    @property
    def key(self) -> tuple[Dimension, str, str]:
        return (self.dimension, self.code, self.sector)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.sector}:{self.dimension.value}:{self.code}={self.amount}"


@dataclass(frozen=True)
class Transaction:
    """An atomic set of postings with an economic ``kind`` and a memo."""

    kind: object
    postings: Sequence[Posting]
    memo: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", code_string(self.kind, what="transaction kind"))
        postings = tuple(self.postings)
        if not postings:
            raise AccountingError("transaction must contain at least one posting")
        if not all(isinstance(posting, Posting) for posting in postings):
            raise AccountingError("transaction postings must be Posting instances")
        object.__setattr__(self, "postings", postings)
        if not isinstance(self.memo, str):
            raise AccountingError("transaction memo must be a string")

    def totals_by_code(self) -> dict[tuple[Dimension, str], Decimal]:
        totals: dict[tuple[Dimension, str], Decimal] = {}
        for posting in self.postings:
            key = (posting.dimension, posting.code)
            totals[key] = totals.get(key, Decimal(0)) + posting.amount
        return totals

    def sector_totals(self) -> dict[str, dict[Dimension, Decimal]]:
        totals: dict[str, dict[Dimension, Decimal]] = {}
        for posting in self.postings:
            row = totals.setdefault(
                posting.sector, {dim: Decimal(0) for dim in Dimension})
            row[posting.dimension] += posting.amount
        return totals


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountSpec:
    """Typed economic role of one account code.

    ``posting_sectors`` restricts who may post to the account at all;
    ``positive_sectors``/``negative_sectors`` restrict the *sign of current and
    capital postings* (real assets allow both signs for acquisition/disposal;
    financial instruments check the sign of the resulting stock instead).
    ``asset_sectors``/``liability_sectors`` define the legal orientation of a
    financial balance; ``mirrored`` means the postings of one transaction must
    sum to zero across sectors for this account.
    """

    code: object
    dimension: object
    posting_sectors: Iterable[object]
    positive_sectors: Iterable[object] = ()
    negative_sectors: Iterable[object] = ()
    mirrored: bool = False
    asset_sectors: Iterable[object] = ()
    liability_sectors: Iterable[object] = ()
    allow_negative_balance: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", code_string(self.code, what="account code"))
        if isinstance(self.dimension, Dimension):
            dimension = self.dimension
        else:
            try:
                dimension = Dimension(code_string(self.dimension, what="dimension"))
            except ValueError as exc:
                raise AccountingError(f"unknown dimension: {self.dimension!r}") from exc
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "posting_sectors",
                           _role_set(self.posting_sectors, what="posting_sectors"))
        object.__setattr__(self, "positive_sectors",
                           _role_set(self.positive_sectors, what="positive_sectors"))
        object.__setattr__(self, "negative_sectors",
                           _role_set(self.negative_sectors, what="negative_sectors"))
        object.__setattr__(self, "asset_sectors",
                           _role_set(self.asset_sectors, what="asset_sectors"))
        object.__setattr__(self, "liability_sectors",
                           _role_set(self.liability_sectors, what="liability_sectors"))
        if not isinstance(self.mirrored, bool):
            raise AccountingError("mirrored must be a bool")
        if not isinstance(self.allow_negative_balance, bool):
            raise AccountingError("allow_negative_balance must be a bool")
        if not self.posting_sectors:
            raise AccountingError(f"account {self.code}: no posting sectors")
        if not self.positive_sectors <= self.posting_sectors:
            raise AccountingError(f"account {self.code}: positive role outside posting sectors")
        if not self.negative_sectors <= self.posting_sectors:
            raise AccountingError(f"account {self.code}: negative role outside posting sectors")
        if not self.asset_sectors <= self.posting_sectors:
            raise AccountingError(f"account {self.code}: asset role outside posting sectors")
        if not self.liability_sectors <= self.posting_sectors:
            raise AccountingError(f"account {self.code}: liability role outside posting sectors")
        if self.asset_sectors & self.liability_sectors:
            raise AccountingError(f"account {self.code}: asset and liability roles overlap")
        if self.dimension is not Dimension.FINANCIAL and (
                self.asset_sectors or self.liability_sectors):
            raise AccountingError(
                f"account {self.code}: asset/liability roles are financial-only")
        if self.dimension is Dimension.FINANCIAL and self.mirrored is False:
            raise AccountingError(
                f"account {self.code}: financial instruments must be mirrored")

    def allows_posting(self, sector: str, amount: Decimal) -> bool:
        if sector not in self.posting_sectors:
            return False
        if self.dimension is Dimension.FINANCIAL:
            # Posting sign is unrestricted (repayments reverse the position);
            # the legal stock orientation is checked after the transaction.
            return True
        if amount > 0:
            return sector in self.positive_sectors
        if amount < 0:
            return sector in self.negative_sectors
        return True


@dataclass(frozen=True)
class ChartOfAccounts:
    """Immutable chart of accounts: sectors, account roles and transaction kinds.

    ``extend`` returns a new chart and is the extension point for stage 3
    (two countries, two currencies) and later stages: new sectors, new
    instruments/flows and new transaction kinds can be added without changing
    the kernel.
    """

    sectors: Iterable[object]
    specs: Mapping[object, AccountSpec]
    kinds: Iterable[object]

    def __post_init__(self) -> None:
        sectors = _role_set(self.sectors, what="sectors")
        if not sectors:
            raise AccountingError("chart must contain at least one sector")
        specs: dict[tuple[Dimension, str], AccountSpec] = {}
        items = self.specs.values() if isinstance(self.specs, Mapping) else self.specs
        for spec in items:
            if not isinstance(spec, AccountSpec):
                raise AccountingError("chart specs must be AccountSpec instances")
            key = (spec.dimension, spec.code)
            if key in specs:
                raise AccountingError(f"duplicate account: {spec.dimension.value}/{spec.code}")
            if not spec.posting_sectors <= sectors:
                raise AccountingError(
                    f"account {spec.code}: posting sector outside the chart")
            specs[key] = spec
        if not specs:
            raise AccountingError("chart must contain at least one account")
        kinds = frozenset(code_string(kind, what="transaction kind")
                          for kind in self.kinds)
        if not kinds:
            raise AccountingError("chart must contain at least one transaction kind")
        object.__setattr__(self, "sectors", sectors)
        object.__setattr__(self, "specs", MappingProxyType(dict(specs)))
        object.__setattr__(self, "kinds", kinds)

    # -- lookups ---------------------------------------------------------
    def lookup(self, dimension: object, code: object) -> AccountSpec | None:
        """Fast account lookup for already-normalised keys; ``None`` if absent."""
        try:
            return self.specs[(dimension, code)]
        except (KeyError, TypeError):
            return None

    def spec(self, dimension: object, code: object) -> AccountSpec:
        try:
            key = (Dimension(code_string(dimension, what="dimension")),
                   code_string(code, what="account code"))
        except ValueError as exc:
            raise AccountingError(f"unknown dimension: {dimension!r}") from exc
        try:
            return self.specs[key]
        except KeyError as exc:
            raise TransactionRejected(
                f"unknown {key[0].value} account: {key[1]}", code="unknown_account",
                details={"dimension": key[0].value, "code": key[1]}) from exc

    def has_spec(self, dimension: object, code: object) -> bool:
        try:
            key = (Dimension(code_string(dimension, what="dimension")),
                   code_string(code, what="account code"))
        except (AccountingError, ValueError):
            return False
        return key in self.specs

    def codes(self, dimension: object) -> tuple[str, ...]:
        dimension = Dimension(code_string(dimension, what="dimension"))
        return tuple(sorted(spec.code for (dim, _), spec in self.specs.items()
                            if dim is dimension))

    def require_sector(self, sector: object) -> str:
        code = code_string(sector, what="sector")
        if code not in self.sectors:
            raise TransactionRejected(
                f"unknown sector: {code}", code="unknown_sector",
                details={"sector": code})
        return code

    def require_kind(self, kind: object) -> str:
        code = code_string(kind, what="transaction kind")
        if code not in self.kinds:
            raise TransactionRejected(
                f"unsupported transaction kind: {code}", code="unknown_kind",
                details={"kind": code})
        return code

    # -- extension -------------------------------------------------------
    def extend(
        self,
        *,
        sectors: Iterable[object] = (),
        specs: Iterable[AccountSpec] = (),
        kinds: Iterable[object] = (),
    ) -> "ChartOfAccounts":
        """Return a new chart with added sectors/accounts/kinds.

        If a supplied spec repeats an existing ``(dimension, code)`` key, the
        role sets are *merged* (union), which is the intended way to give a new
        sector access to an existing account.  Use an explicit new
        :class:`AccountSpec` when the roles must be redefined.
        """
        new_sectors = self.sectors | _role_set(sectors, what="sectors")
        new_specs: dict[tuple[Dimension, str], AccountSpec] = dict(self.specs)
        for spec in specs:
            if not isinstance(spec, AccountSpec):
                raise AccountingError("chart extension specs must be AccountSpec instances")
            key = (spec.dimension, spec.code)
            if key in new_specs:
                new_specs[key] = _merge_specs(new_specs[key], spec)
            else:
                new_specs[key] = spec
        new_kinds = self.kinds | frozenset(
            code_string(kind, what="transaction kind") for kind in kinds)
        return ChartOfAccounts(new_sectors, new_specs, new_kinds)


def _merge_specs(old: AccountSpec, new: AccountSpec) -> AccountSpec:
    if old.dimension is not new.dimension or old.code != new.code:
        raise AccountingError("cannot merge different accounts")
    return AccountSpec(
        code=old.code,
        dimension=old.dimension,
        posting_sectors=old.posting_sectors | new.posting_sectors,
        positive_sectors=old.positive_sectors | new.positive_sectors,
        negative_sectors=old.negative_sectors | new.negative_sectors,
        mirrored=old.mirrored or new.mirrored,
        asset_sectors=old.asset_sectors | new.asset_sectors,
        liability_sectors=old.liability_sectors | new.liability_sectors,
        allow_negative_balance=(old.allow_negative_balance
                                or new.allow_negative_balance),
    )


_ALL_SECTORS = tuple(sector.value for sector in Sector)
_HOUSEHOLD, _FIRM, _BANK, _GOVERNMENT = _ALL_SECTORS


def build_default_chart() -> ChartOfAccounts:
    """The documented four-sector, one-currency, price=1 chart of accounts."""
    specs = (
        # Current account: output and consumption have no counterpart current
        # entry inside the kernel; their counterpart is a real asset change.
        AccountSpec(CurrentFlow.OUTPUT, Dimension.CURRENT,
                    posting_sectors=(_FIRM,), positive_sectors=(_FIRM,)),
        AccountSpec(CurrentFlow.WAGES, Dimension.CURRENT,
                    posting_sectors=(_FIRM, _GOVERNMENT, _HOUSEHOLD),
                    positive_sectors=(_HOUSEHOLD,),
                    negative_sectors=(_FIRM, _GOVERNMENT), mirrored=True),
        AccountSpec(CurrentFlow.DIRECT_TAX, Dimension.CURRENT,
                    posting_sectors=(_FIRM, _HOUSEHOLD, _GOVERNMENT),
                    positive_sectors=(_GOVERNMENT,),
                    negative_sectors=(_FIRM, _HOUSEHOLD), mirrored=True),
        AccountSpec(CurrentFlow.TRANSFER, Dimension.CURRENT,
                    posting_sectors=_ALL_SECTORS,
                    positive_sectors=_ALL_SECTORS,
                    negative_sectors=_ALL_SECTORS, mirrored=True),
        AccountSpec(CurrentFlow.INTEREST, Dimension.CURRENT,
                    posting_sectors=_ALL_SECTORS,
                    positive_sectors=(_BANK, _HOUSEHOLD),
                    negative_sectors=(_FIRM, _GOVERNMENT), mirrored=True),
        AccountSpec(CurrentFlow.HOUSEHOLD_CONSUMPTION, Dimension.CURRENT,
                    posting_sectors=(_HOUSEHOLD,), negative_sectors=(_HOUSEHOLD,)),
        AccountSpec(CurrentFlow.GOVERNMENT_CONSUMPTION, Dimension.CURRENT,
                    posting_sectors=(_GOVERNMENT,), negative_sectors=(_GOVERNMENT,)),
        AccountSpec(CurrentFlow.DEPRECIATION, Dimension.CURRENT,
                    posting_sectors=(_FIRM,), negative_sectors=(_FIRM,)),
        # Capital account.
        AccountSpec(CapitalFlow.CAPITAL_TRANSFER, Dimension.CAPITAL,
                    posting_sectors=_ALL_SECTORS,
                    positive_sectors=_ALL_SECTORS,
                    negative_sectors=_ALL_SECTORS, mirrored=True),
        # Real assets.
        AccountSpec(RealAsset.INVENTORY, Dimension.REAL,
                    posting_sectors=(_FIRM,),
                    positive_sectors=(_FIRM,), negative_sectors=(_FIRM,)),
        AccountSpec(RealAsset.FIXED_CAPITAL, Dimension.REAL,
                    posting_sectors=(_FIRM,),
                    positive_sectors=(_FIRM,), negative_sectors=(_FIRM,)),
        # Financial account: signed stocks, mirrored within every transaction.
        AccountSpec(FinancialInstrument.DEPOSIT, Dimension.FINANCIAL,
                    posting_sectors=_ALL_SECTORS, mirrored=True,
                    asset_sectors=(_HOUSEHOLD, _FIRM, _GOVERNMENT),
                    liability_sectors=(_BANK,)),
        AccountSpec(FinancialInstrument.LOAN, Dimension.FINANCIAL,
                    posting_sectors=(_BANK, _FIRM), mirrored=True,
                    asset_sectors=(_BANK,), liability_sectors=(_FIRM,)),
        AccountSpec(FinancialInstrument.BOND, Dimension.FINANCIAL,
                    posting_sectors=(_BANK, _GOVERNMENT), mirrored=True,
                    asset_sectors=(_BANK,), liability_sectors=(_GOVERNMENT,)),
        AccountSpec(FinancialInstrument.RECEIVABLE, Dimension.FINANCIAL,
                    posting_sectors=(_FIRM, _HOUSEHOLD), mirrored=True,
                    asset_sectors=(_HOUSEHOLD,), liability_sectors=(_FIRM,)),
    )
    kinds = tuple(kind.value for kind in TransactionKind)
    return ChartOfAccounts(_ALL_SECTORS, specs, kinds)


DEFAULT_CHART = build_default_chart()


# ---------------------------------------------------------------------------
# Immutable results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BalanceSheet:
    """Signed financial positions, real assets and net worth of one sector."""

    sector: object
    financial: Mapping[str, object]
    real: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sector", code_string(self.sector, what="sector"))
        object.__setattr__(self, "financial", MappingProxyType({
            code_string(code, what="instrument"): to_decimal(amount)
            for code, amount in dict(self.financial).items()}))
        object.__setattr__(self, "real", MappingProxyType({
            code_string(code, what="asset"): to_decimal(amount)
            for code, amount in dict(self.real).items()}))

    def get_financial(self, instrument: object) -> Decimal:
        return self.financial.get(code_string(instrument, what="instrument"), Decimal(0))

    def get_real(self, asset: object) -> Decimal:
        return self.real.get(code_string(asset, what="asset"), Decimal(0))

    @property
    def net_financial(self) -> Decimal:
        return sum(self.financial.values(), Decimal(0))

    @property
    def net_worth(self) -> Decimal:
        return self.net_financial + sum(self.real.values(), Decimal(0))


@dataclass(frozen=True)
class Snapshot:
    """Balance sheets of all sectors at one moment (closing of a period)."""

    period: str
    balances: Mapping[str, BalanceSheet]

    def __post_init__(self) -> None:
        balances = {code_string(sector, what="sector"): sheet
                    for sector, sheet in dict(self.balances).items()}
        for sector, sheet in balances.items():
            if not isinstance(sheet, BalanceSheet):
                raise AccountingError("snapshot balances must be BalanceSheet instances")
            if sheet.sector != sector:
                raise AccountingError(
                    f"snapshot key {sector!r} does not match balance sheet sector {sheet.sector!r}")
        object.__setattr__(self, "period", code_string(self.period, what="period"))
        object.__setattr__(self, "balances", MappingProxyType(balances))

    def sheet(self, sector: object) -> BalanceSheet:
        code = code_string(sector, what="sector")
        try:
            return self.balances[code]
        except KeyError as exc:
            raise AccountingError(f"unknown sector in snapshot: {code}") from exc

    def net_worth(self, sector: object) -> Decimal:
        return self.sheet(sector).net_worth

    def financial(self, sector: object, instrument: object) -> Decimal:
        return self.sheet(sector).get_financial(instrument)

    def real(self, sector: object, asset: object) -> Decimal:
        return self.sheet(sector).get_real(asset)

    def instrument_balances(self, instrument: object) -> dict[str, Decimal]:
        code = code_string(instrument, what="instrument")
        return {sector: sheet.financial.get(code, Decimal(0))
                for sector, sheet in self.balances.items()}

    def total_net_worth(self) -> Decimal:
        return sum((sheet.net_worth for sheet in self.balances.values()), Decimal(0))

    def total_real_assets(self) -> Decimal:
        return sum((sum(sheet.real.values(), Decimal(0))
                    for sheet in self.balances.values()), Decimal(0))


@dataclass(frozen=True)
class JournalEntry:
    """One committed atomic transaction."""

    sequence: int
    period: str
    kind: str
    postings: Sequence[Posting]
    memo: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise AccountingError("journal sequence must be an integer")
        object.__setattr__(self, "period", code_string(self.period, what="period"))
        object.__setattr__(self, "kind", code_string(self.kind, what="transaction kind"))
        postings = tuple(self.postings)
        if not all(isinstance(posting, Posting) for posting in postings):
            raise AccountingError("journal postings must be Posting instances")
        object.__setattr__(self, "postings", postings)
        object.__setattr__(self, "memo", str(self.memo))

    def total(self, dimension: object | None = None) -> Decimal:
        dimension = Dimension(dimension) if dimension is not None else None
        return sum((posting.amount for posting in self.postings
                    if dimension is None or posting.dimension is dimension), Decimal(0))

    def sector_total(self, sector: object, dimension: object | None = None) -> Decimal:
        code = code_string(sector, what="sector")
        dimension = Dimension(dimension) if dimension is not None else None
        return sum((posting.amount for posting in self.postings
                    if posting.sector == code
                    and (dimension is None or posting.dimension is dimension)),
                   Decimal(0))


@dataclass(frozen=True)
class NonTransactionEntry:
    """Revaluation or other volume change: a change of stocks outside the journal."""

    sequence: int
    period: str
    category: str
    sector: str
    dimension: Dimension
    code: str
    amount: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise AccountingError("non-transaction sequence must be an integer")
        object.__setattr__(self, "period", code_string(self.period, what="period"))
        category = code_string(self.category, what="category")
        if category not in ("revaluation", "other_volume"):
            raise AccountingError(f"unknown non-transaction category: {category}")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "sector", code_string(self.sector, what="sector"))
        if isinstance(self.dimension, Dimension):
            dimension = self.dimension
        else:
            dimension = Dimension(code_string(self.dimension, what="dimension"))
        if dimension not in (Dimension.REAL, Dimension.FINANCIAL):
            raise AccountingError("non-transaction changes are real or financial only")
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "code", code_string(self.code, what="account code"))
        object.__setattr__(self, "amount", to_decimal(self.amount))


@dataclass(frozen=True)
class PeriodReport:
    """Complete closing report of one accounting period.

    All mappings are keyed by sector code; ``current`` and ``financial_change``
    are nested ``sector -> account -> amount``.  Reports are immutable by
    convention (the mappings are plain dicts for convenient comparison, but
    callers must not mutate them).
    """

    period: str
    opening_net_worth: Mapping[str, Decimal]
    closing_net_worth: Mapping[str, Decimal]
    current: Mapping[str, Mapping[str, Decimal]]
    savings: Mapping[str, Decimal]
    consumption_of_fixed_capital: Mapping[str, Decimal]
    gross_saving: Mapping[str, Decimal]
    capital_transfers: Mapping[str, Decimal]
    real_change: Mapping[str, Mapping[str, Decimal]]
    inventory_change: Mapping[str, Decimal]
    fixed_capital_change: Mapping[str, Decimal]
    gross_fixed_capital_formation: Mapping[str, Decimal]
    capital_formation: Mapping[str, Decimal]
    financial_change: Mapping[str, Mapping[str, Decimal]]
    net_financial_change: Mapping[str, Decimal]
    net_lending: Mapping[str, Decimal]
    revaluation_real: Mapping[str, Decimal]
    revaluation_financial: Mapping[str, Decimal]
    revaluation: Mapping[str, Decimal]
    other_volume: Mapping[str, Decimal]
    output: Decimal
    household_consumption: Decimal
    government_consumption: Decimal
    wages_received: Decimal
    gross_operating_surplus: Decimal
    gdp_production: Decimal
    gdp_expenditure: Decimal
    gdp_income: Decimal
    entries: Sequence[JournalEntry]
    nontransaction: Sequence[NonTransactionEntry]
    snapshot: Snapshot

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", code_string(self.period, what="period"))
        entries = tuple(self.entries)
        nontransaction = tuple(self.nontransaction)
        if not all(isinstance(entry, JournalEntry) for entry in entries):
            raise AccountingError("report entries must be JournalEntry instances")
        if not all(isinstance(entry, NonTransactionEntry) for entry in nontransaction):
            raise AccountingError("report non-transaction entries must be NonTransactionEntry")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "nontransaction", nontransaction)

    def current_flow(self, sector: object, flow: object) -> Decimal:
        sector_code = code_string(sector, what="sector")
        flow_code = code_string(flow, what="flow")
        return self.current.get(sector_code, {}).get(flow_code, Decimal(0))

    def financial_flow(self, sector: object, instrument: object) -> Decimal:
        sector_code = code_string(sector, what="sector")
        instrument_code = code_string(instrument, what="instrument")
        return self.financial_change.get(
            sector_code, {}).get(instrument_code, Decimal(0))

    @property
    def sectors(self) -> tuple[str, ...]:
        return tuple(sorted(self.closing_net_worth))

    def as_dict(self) -> dict[str, object]:
        """JSON-ready representation (every Decimal becomes a string)."""
        def dec(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, Mapping):
                return {str(key): dec(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [dec(item) for item in value]
            return value

        return {
            "period": self.period,
            "opening_net_worth": dec(self.opening_net_worth),
            "closing_net_worth": dec(self.closing_net_worth),
            "current": dec(self.current),
            "savings": dec(self.savings),
            "consumption_of_fixed_capital": dec(self.consumption_of_fixed_capital),
            "gross_saving": dec(self.gross_saving),
            "capital_transfers": dec(self.capital_transfers),
            "inventory_change": dec(self.inventory_change),
            "fixed_capital_change": dec(self.fixed_capital_change),
            "gross_fixed_capital_formation": dec(self.gross_fixed_capital_formation),
            "capital_formation": dec(self.capital_formation),
            "financial_change": dec(self.financial_change),
            "net_financial_change": dec(self.net_financial_change),
            "net_lending": dec(self.net_lending),
            "revaluation": dec(self.revaluation),
            "revaluation_real": dec(self.revaluation_real),
            "revaluation_financial": dec(self.revaluation_financial),
            "other_volume": dec(self.other_volume),
            "output": str(self.output),
            "household_consumption": str(self.household_consumption),
            "government_consumption": str(self.government_consumption),
            "wages_received": str(self.wages_received),
            "gross_operating_surplus": str(self.gross_operating_surplus),
            "gdp_production": str(self.gdp_production),
            "gdp_expenditure": str(self.gdp_expenditure),
            "gdp_income": str(self.gdp_income),
            "entries": [
                {"sequence": entry.sequence, "period": entry.period,
                 "kind": entry.kind, "memo": entry.memo,
                 "postings": [{"sector": p.sector, "dimension": p.dimension.value,
                               "code": p.code, "amount": str(p.amount)}
                              for p in entry.postings]}
                for entry in self.entries],
            "nontransaction": [
                {"sequence": entry.sequence, "period": entry.period,
                 "category": entry.category, "sector": entry.sector,
                 "dimension": entry.dimension.value, "code": entry.code,
                 "amount": str(entry.amount)}
                for entry in self.nontransaction],
            "closing_balance_sheet": {
                sector: {"financial": dec(sheet.financial),
                         "real": dec(sheet.real),
                         "net_worth": str(sheet.net_worth)}
                for sector, sheet in self.snapshot.balances.items()},
        }


__all__ = [
    "AccountingError", "TransactionRejected", "InvariantViolation",
    "PeriodStateError", "to_decimal", "code_string",
    "Sector", "Dimension", "CurrentFlow", "CapitalFlow", "RealAsset",
    "FinancialInstrument", "TransactionKind",
    "Posting", "Transaction", "AccountSpec", "ChartOfAccounts",
    "DEFAULT_CHART", "build_default_chart",
    "BalanceSheet", "Snapshot", "JournalEntry", "NonTransactionEntry",
    "PeriodReport",
]
