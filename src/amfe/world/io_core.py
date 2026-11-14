"""Static multi-regional input-output core (P2): balance, productiveness, aggregation, scenarios.

Scope
-----
This module implements the *static* part of the MRIO layer described in docs/05 (country x
industry balances), docs/34 (mathematical architecture) and docs/53 (equation registry,
``EQ-IO-001``: ``x = A x + y``); aggregation follows docs/38 I05/I08 (no double counting, explicit
classification map).  It deliberately contains no dynamics, no prices and no forecasting: one
instance is one technology snapshot, one valuation base and one period.

Dataset contract
----------------
A dataset is a mapping with the keys ``{"countries", "industries", "Z", "y", "x", "va", "units"}``:

* ``countries``, ``industries`` — non-empty unique label lists; ``n = len(countries)*len(industries)``;
* ``Z`` — ``n x n`` intermediate-use matrix in the country-major ``(country, industry)`` order,
  ``index(country_c, industry_i) = c*len(industries) + i``; ``Z[i][j]`` is product ``i`` used by
  industry ``j``;
* ``y`` — final demand by product; ``x`` — gross output; ``va`` — value added by industry;
* ``units`` — one consistent value base (default ``"USD_mn"``).

Accounting identities (checked fail-closed, no silent repairs):

    Z 1 + y = x            (product balance, row sums)
    Z^T 1 + va = x          (industry value-added balance, column sums)

The second identity is the value-added closure of the same flow table, not a second table: in a
consistent one-valuation snapshot both hold simultaneously.

What is deliberately not here
-----------------------------
* no deflators, exchange rates or price equations: all entries are in one value base;
* no time dimension (``EQ-IO-002`` is a hypothesis and is not implemented);
* no statistical-discrepancy balancing: an inconsistent table raises :class:`IOCoreError`
  instead of being rebalanced;
* no claim that the coefficients are constant across snapshots.

Public API
----------
``IODataset``       validated dataset: labels, index map, balances, technical matrix, aggregation.
``LeontiefCore``    validated ``A``: spectral radius, Hawkins-Simon, inverse, solve, iteration.
``final_demand_shock`` / ``technology_shock``  scenario results (plain dicts with numbers).
``check_dataset``   fail-closed balance report without building a core.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np

TOLERANCE = 1e-9
DEFAULT_UNITS = "USD_mn"
LEVELS = ("country", "industry")


class IOCoreError(ValueError):
    """Invalid IO data, a non-productive technology or an infeasible scenario."""


# --------------------------------------------------------------------------------- input coercion
def _as_vector(values: Iterable[float], size: int | None, name: str, *,
               non_negative: bool = True, tolerance: float = TOLERANCE,
               strictly_positive: bool = False) -> np.ndarray:
    try:
        array = np.asarray([float(value) for value in values], dtype=float)
    except (TypeError, ValueError) as exc:
        raise IOCoreError(f"{name} must be a sequence of numbers") from exc
    if array.ndim != 1 or array.size == 0:
        raise IOCoreError(f"{name} must be a non-empty one-dimensional sequence")
    if size is not None and array.size != int(size):
        raise IOCoreError(f"{name} must have {int(size)} entries, got {array.size}")
    if not np.all(np.isfinite(array)):
        raise IOCoreError(f"{name} contains non-finite values")
    if non_negative and np.any(array < -tolerance):
        raise IOCoreError(f"{name} must be non-negative, got {array.tolist()}")
    if strictly_positive and np.any(array <= tolerance):
        raise IOCoreError(
            f"{name} must be strictly positive: the technical coefficients are formed by dividing "
            f"by gross output and x <= 0 has no economic meaning, got {array.tolist()}")
    return array


def _as_matrix(rows: Sequence[Sequence[float]], name: str, *,
               non_negative: bool = True, tolerance: float = TOLERANCE) -> np.ndarray:
    try:
        matrix = np.asarray([list(row) for row in rows], dtype=float)
    except (TypeError, ValueError) as exc:
        raise IOCoreError(f"{name} must be a matrix of numbers") from exc
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise IOCoreError(f"{name} must be a non-empty matrix")
    if matrix.shape[0] != matrix.shape[1]:
        raise IOCoreError(f"{name} must be square, got shape {matrix.shape}")
    if not np.all(np.isfinite(matrix)):
        raise IOCoreError(f"{name} contains non-finite values")
    if non_negative and np.any(matrix < -tolerance):
        raise IOCoreError(f"{name} must be non-negative (no negative flows)")
    return matrix


def _labels(values: Iterable[object], name: str) -> tuple[str, ...]:
    labels = tuple(str(value).strip() for value in values)
    if not labels:
        raise IOCoreError(f"{name} must not be empty")
    if any(not label for label in labels):
        raise IOCoreError(f"{name} must not contain empty labels")
    if len(set(labels)) != len(labels):
        raise IOCoreError(f"{name} must be unique")
    return labels


# --------------------------------------------------------------------------------------- dataset
@dataclass(frozen=True)
class IODataset:
    """A validated country x industry input-output snapshot (one valuation base, one period)."""

    countries: tuple[str, ...]
    industries: tuple[str, ...]
    Z: tuple[tuple[float, ...], ...]
    y: tuple[float, ...]
    x: tuple[float, ...]
    va: tuple[float, ...]
    units: str = DEFAULT_UNITS
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        countries = _labels(self.countries, "countries")
        industries = _labels(self.industries, "industries")
        tolerance = float(self.tolerance)
        if not math.isfinite(tolerance) or tolerance <= 0.0:
            raise IOCoreError("tolerance must be a positive finite number")
        size = len(countries) * len(industries)
        Z = _as_matrix(self.Z, "Z", non_negative=True, tolerance=tolerance)
        if Z.shape != (size, size):
            raise IOCoreError(
                f"Z must be {size} x {size} for {len(countries)} countries x "
                f"{len(industries)} industries, got {Z.shape}")
        y = _as_vector(self.y, size, "y", non_negative=True, tolerance=tolerance)
        x = _as_vector(self.x, size, "x", non_negative=True, tolerance=tolerance,
                       strictly_positive=True)
        va = _as_vector(self.va, size, "va", non_negative=True, tolerance=tolerance)
        units = str(self.units).strip()
        if not units:
            raise IOCoreError("units must be a non-empty string")
        object.__setattr__(self, "countries", countries)
        object.__setattr__(self, "industries", industries)
        object.__setattr__(self, "Z", tuple(tuple(float(value) for value in row) for row in Z))
        object.__setattr__(self, "y", tuple(float(value) for value in y))
        object.__setattr__(self, "x", tuple(float(value) for value in x))
        object.__setattr__(self, "va", tuple(float(value) for value in va))
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "tolerance", tolerance)
        self.check_balance()

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, dataset: Mapping) -> "IODataset":
        """Build a dataset from the documented mapping; refuses missing or extra structure."""
        if not isinstance(dataset, Mapping):
            raise IOCoreError("dataset must be a mapping with the keys countries/industries/Z/y/x/va/units")
        required = {"countries", "industries", "Z", "y", "x", "va", "units"}
        missing = sorted(required - set(dataset))
        if missing:
            raise IOCoreError(f"dataset is missing keys: {missing}")
        return cls(countries=tuple(dataset["countries"]), industries=tuple(dataset["industries"]),
                   Z=dataset["Z"], y=dataset["y"], x=dataset["x"], va=dataset["va"],
                   units=dataset["units"],
                   tolerance=float(dataset.get("tolerance", TOLERANCE)))

    def to_dict(self) -> dict:
        return {"countries": list(self.countries), "industries": list(self.industries),
                "Z": [list(row) for row in self.Z], "y": list(self.y), "x": list(self.x),
                "va": list(self.va), "units": self.units}

    # -- labels and indexing -----------------------------------------------------------
    @property
    def n_countries(self) -> int:
        return len(self.countries)

    @property
    def n_industries(self) -> int:
        return len(self.industries)

    @property
    def size(self) -> int:
        return self.n_countries * self.n_industries

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(f"{country}/{industry}" for country in self.countries
                     for industry in self.industries)

    def index(self, country: str, industry: str) -> int:
        try:
            c = self.countries.index(str(country))
        except ValueError as exc:
            raise IOCoreError(f"unknown country {country!r}") from exc
        try:
            i = self.industries.index(str(industry))
        except ValueError as exc:
            raise IOCoreError(f"unknown industry {industry!r}") from exc
        return c * self.n_industries + i

    def country_of(self, index: int) -> str:
        return self.countries[self._check_index(index) // self.n_industries]

    def industry_of(self, index: int) -> str:
        return self.industries[self._check_index(index) % self.n_industries]

    def _check_index(self, index: int) -> int:
        index = int(index)
        if not 0 <= index < self.size:
            raise IOCoreError(f"index {index} is outside 0..{self.size - 1}")
        return index

    # -- balances -----------------------------------------------------------------------
    def _array(self, name: str) -> np.ndarray:
        return np.asarray(getattr(self, name), dtype=float)

    def row_totals(self) -> list[float]:
        """``Z 1``: intermediate use of each product."""
        return np.asarray(self.Z, dtype=float).sum(axis=1).tolist()

    def column_totals(self) -> list[float]:
        """``Z^T 1``: intermediate inputs of each industry."""
        return np.asarray(self.Z, dtype=float).sum(axis=0).tolist()

    def balance_residual(self) -> dict:
        """``Z 1 + y - x`` and ``Z^T 1 + va - x``: both must be zero componentwise."""
        Z = np.asarray(self.Z, dtype=float)
        product = Z.sum(axis=1) + self._array("y") - self._array("x")
        value_added = Z.sum(axis=0) + self._array("va") - self._array("x")
        return {"product": product.tolist(), "value_added": value_added.tolist(),
                "max_abs": float(max(np.max(np.abs(product)), np.max(np.abs(value_added))))}

    def check_balance(self, tolerance: float | None = None) -> float:
        """Raise unless both identities hold; returns the largest absolute residual.

        The comparison is relative to ``max(1, |x|_inf)`` so that very large tables are not
        rejected by floating-point rounding at the scale of the data.
        """
        tol = self.tolerance if tolerance is None else float(tolerance)
        residual = self.balance_residual()
        scale = max(1.0, float(np.max(self._array("x"))))
        worst = residual["max_abs"]
        if worst > tol * scale:
            raise IOCoreError(
                "input-output table is inconsistent: max |Z 1 + y - x| or |Z^T 1 + va - x| = "
                f"{worst:.6e} > tolerance {tol * scale:.3e}; the dataset must satisfy both "
                "balances exactly (no silent rebalancing)")
        return worst

    # -- technology ---------------------------------------------------------------------
    def technical_matrix(self) -> list[list[float]]:
        """``A = Z diag(x)^-1``: ``A[i][j] = Z[i][j] / x[j]``; raises when ``x <= 0``."""
        x = self._array("x")
        if np.any(x <= self.tolerance):
            raise IOCoreError("gross output x must be strictly positive for A = Z / x")
        Z = np.asarray(self.Z, dtype=float)
        A = Z / x[np.newaxis, :]
        if np.any(A < -self.tolerance):
            raise IOCoreError("technical coefficients are negative")
        return A.tolist()

    def leontief(self) -> "LeontiefCore":
        return LeontiefCore.from_dataset(self)

    # -- aggregation (docs/38 I08: explicit map, no double counting) ---------------------
    def aggregate(self, level: str = "country") -> "IODataset":
        """Aggregate the table to ``level`` (``country`` or ``industry``) preserving totals.

        Country level: ``countries`` kept, ``industries == ("TOTAL",)`` (``n_i = 1``, so the
        aggregated matrix is ``n_c x n_c``).  Industry level: ``countries == ("WORLD",)``.
        The aggregation is additive in the common valuation base, so ``Z``, ``y``, ``x`` and
        ``va`` totals and both balances are preserved.
        """
        if level not in LEVELS:
            raise IOCoreError(f"level must be one of {LEVELS}, got {level!r}")
        nc, ni = self.n_countries, self.n_industries
        Z = np.asarray(self.Z, dtype=float)
        y = self._array("y").reshape(nc, ni)
        x = self._array("x").reshape(nc, ni)
        va = self._array("va").reshape(nc, ni)
        block = Z.reshape(nc, ni, nc, ni)
        if level == "country":
            Z_agg = block.sum(axis=(1, 3))
            return IODataset(countries=self.countries, industries=("TOTAL",),
                             Z=Z_agg.tolist(), y=y.sum(axis=1).tolist(), x=x.sum(axis=1).tolist(),
                             va=va.sum(axis=1).tolist(), units=self.units,
                             tolerance=self.tolerance)
        Z_agg = block.sum(axis=(0, 2))
        return IODataset(countries=("WORLD",), industries=self.industries,
                         Z=Z_agg.tolist(), y=y.sum(axis=0).tolist(), x=x.sum(axis=0).tolist(),
                         va=va.sum(axis=0).tolist(), units=self.units, tolerance=self.tolerance)

    def summarize(self) -> dict:
        """Metadata-only summary (no raw matrix)."""
        x = self._array("x")
        return {"units": self.units, "countries": self.n_countries,
                "industries": self.n_industries, "sectors": self.size,
                "total_gross_output": round(float(x.sum()), 9),
                "total_final_demand": round(float(self._array("y").sum()), 9),
                "total_value_added": round(float(self._array("va").sum()), 9),
                "share_intermediate": round(float(np.asarray(self.Z).sum() / x.sum()), 9),
                "max_abs_balance_residual": round(self.check_balance(), 12)}


def check_dataset(dataset: Mapping) -> dict:
    """Validate a dataset mapping without building a core; returns its metadata summary."""
    return IODataset.from_dict(dataset).summarize()


# ------------------------------------------------------------------------------ aggregation API
def _dataset_and_values(dataset: IODataset, values: Sequence[float], name: str) -> np.ndarray:
    if not isinstance(dataset, IODataset):
        raise IOCoreError("dataset must be an IODataset")
    array = _as_vector(values, dataset.size, name, non_negative=False)
    return array


def aggregate_vector_by_country(dataset: IODataset, values: Sequence[float]) -> list[float]:
    """Sum a sector vector over industries inside each country."""
    array = _dataset_and_values(dataset, values, "values")
    return array.reshape(dataset.n_countries, dataset.n_industries).sum(axis=1).tolist()


def aggregate_vector_by_industry(dataset: IODataset, values: Sequence[float]) -> list[float]:
    """Sum a sector vector over countries inside each industry."""
    array = _dataset_and_values(dataset, values, "values")
    return array.reshape(dataset.n_countries, dataset.n_industries).sum(axis=0).tolist()


def aggregate_matrix_by_country(dataset: IODataset, matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """Sum an ``(n x n)`` sector flow matrix into a country x country matrix."""
    if not isinstance(dataset, IODataset):
        raise IOCoreError("dataset must be an IODataset")
    flow = _as_matrix(matrix, "matrix", non_negative=False)
    if flow.shape != (dataset.size, dataset.size):
        raise IOCoreError(f"matrix must be {dataset.size} x {dataset.size}, got {flow.shape}")
    nc, ni = dataset.n_countries, dataset.n_industries
    return flow.reshape(nc, ni, nc, ni).sum(axis=(1, 3)).tolist()


def aggregate_matrix_by_industry(dataset: IODataset, matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """Sum an ``(n x n)`` sector flow matrix into an industry x industry matrix."""
    if not isinstance(dataset, IODataset):
        raise IOCoreError("dataset must be an IODataset")
    flow = _as_matrix(matrix, "matrix", non_negative=False)
    if flow.shape != (dataset.size, dataset.size):
        raise IOCoreError(f"matrix must be {dataset.size} x {dataset.size}, got {flow.shape}")
    nc, ni = dataset.n_countries, dataset.n_industries
    return flow.reshape(nc, ni, nc, ni).sum(axis=(0, 2)).tolist()


# --------------------------------------------------------------------------- Leontief algebra
@dataclass(frozen=True)
class LeontiefCore:
    """A validated direct-requirements matrix ``A`` (``A[i][j]`` units of i per unit of j)."""

    A: tuple[tuple[float, ...], ...]
    labels: tuple[str, ...]
    units: str = DEFAULT_UNITS
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        A = _as_matrix(self.A, "A", non_negative=True, tolerance=float(self.tolerance))
        labels = tuple(str(label) for label in self.labels)
        if len(labels) != A.shape[0]:
            raise IOCoreError(f"labels must have one entry per row ({A.shape[0]}), got {len(labels)}")
        if len(set(labels)) != len(labels):
            raise IOCoreError("labels must be unique")
        tolerance = float(self.tolerance)
        if not math.isfinite(tolerance) or tolerance <= 0.0:
            raise IOCoreError("tolerance must be a positive finite number")
        object.__setattr__(self, "A", tuple(tuple(float(value) for value in row) for row in A))
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "tolerance", tolerance)
        object.__setattr__(self, "units", str(self.units).strip() or DEFAULT_UNITS)

    @classmethod
    def from_dataset(cls, dataset: IODataset) -> "LeontiefCore":
        if not isinstance(dataset, IODataset):
            raise IOCoreError("dataset must be an IODataset")
        return cls(tuple(tuple(row) for row in dataset.technical_matrix()), dataset.labels,
                   units=dataset.units, tolerance=dataset.tolerance)

    # -- basics -------------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self.labels)

    def matrix(self) -> list[list[float]]:
        return [list(row) for row in self.A]

    def leontief_matrix(self) -> list[list[float]]:
        """``I - A`` as a plain list of lists."""
        matrix = np.eye(self.size) - np.asarray(self.A, dtype=float)
        return matrix.tolist()

    # -- productiveness -----------------------------------------------------------------
    def spectral_radius(self) -> float:
        """Perron-Frobenius root of ``A`` (``max |eigenvalue|``), computed from eigenvalues.

        ``A >= 0``, so the radius is the Perron root and ``rho(A) < 1`` is equivalent to
        ``(I - A)^-1 = sum_k A^k >= 0`` being convergent (docs/53, EQ-IO-001).
        """
        eigenvalues = np.linalg.eigvals(np.asarray(self.A, dtype=float))
        return float(np.max(np.abs(eigenvalues))) if eigenvalues.size else 0.0

    def is_productive(self) -> bool:
        """``rho(A) < 1`` within the tolerance; the boundary is treated as non-productive."""
        return self.spectral_radius() < 1.0 - self.tolerance

    def productiveness_margin(self) -> float:
        return 1.0 - self.spectral_radius()

    def hawkins_simon(self) -> dict:
        """Hawkins-Simon condition: all leading principal minors of ``(I - A)`` are positive.

        The minors are obtained from an LU factorisation *without* pivoting: the leading
        principal minor of order ``k`` is the product of the first ``k`` diagonal pivots, so
        positivity of every pivot is exactly Hawkins-Simon.  A zero pivot is a zero leading
        minor, which fails the condition (the system is singular or on the boundary).
        For small matrices the minors are also reported explicitly.
        """
        size = self.size
        M = np.eye(size) - np.asarray(self.A, dtype=float)
        U = M.copy()
        scale = max(1.0, float(np.max(np.abs(M)))) if M.size else 1.0
        pivot_tolerance = self.tolerance * scale
        pivots: list[float] = []
        first_failure: int | None = None
        for column in range(size):
            pivot = float(U[column, column])
            pivots.append(pivot)
            if abs(pivot) <= pivot_tolerance:
                first_failure = column
                break
            if column + 1 < size:
                factors = U[column + 1:, column] / pivot
                U[column + 1:, column:] -= np.outer(factors, U[column, column:])
        pivots_positive = all(pivot > pivot_tolerance for pivot in pivots)
        passed = first_failure is None and pivots_positive and len(pivots) == size
        minors = None
        if size <= 32:
            minors = [float(np.linalg.det(M[:order, :order])) for order in range(1, size + 1)]
        return {"pass": bool(passed), "method": "LU_WITHOUT_PIVOTING_LEADING_PRINCIPAL_MINORS",
                "leading_principal_minors": minors, "n_minors": size,
                "min_pivot": min(pivots) if pivots else None,
                "pivots_positive": bool(pivots_positive),
                "first_nonpositive_pivot": first_failure,
                "note": "all leading principal minors of (I - A) must be positive; for A >= 0 "
                        "this is equivalent to rho(A) < 1"}

    # -- inverse and solution -----------------------------------------------------------
    def inverse(self) -> list[list[float]]:
        """``(I - A)^-1``; refuses non-productive or singular technology (fail-closed)."""
        if not self.is_productive():
            raise IOCoreError(
                "A is not productive: rho(A) = %.9f >= 1, so (I - A)^-1 = sum_k A^k does not "
                "converge and the material balance has no non-negative solution for y >= 0"
                % self.spectral_radius())
        inverse = np.linalg.inv(np.eye(self.size) - np.asarray(self.A, dtype=float))
        if not np.all(np.isfinite(inverse)):
            raise IOCoreError("inverse of (I - A) is not finite")
        if np.any(inverse < -self.tolerance):
            raise IOCoreError(
                "inverse of (I - A) has negative entries: the technology is not productive")
        return inverse.tolist()

    def matrix_inverse_product(self) -> float:
        """``max |(I - A) (I - A)^-1 - I|``: numerical check of the inverse."""
        M = np.eye(self.size) - np.asarray(self.A, dtype=float)
        product = M @ np.asarray(self.inverse(), dtype=float)
        return float(np.max(np.abs(product - np.eye(self.size))))

    def gross_output(self, final_demand: Sequence[float]) -> list[float]:
        """``x = (I - A)^-1 y`` for finite demand; refuses a negative gross-output solution."""
        demand = _as_vector(final_demand, self.size, "final_demand", non_negative=False,
                            tolerance=self.tolerance)
        solution = np.asarray(self.inverse(), dtype=float) @ demand
        if np.any(solution < -self.tolerance * max(1.0, float(np.max(np.abs(demand))))):
            raise IOCoreError(
                "infeasible final demand: the solution has negative gross output "
                f"(min = {float(np.min(solution)):.6e}); an economy cannot dis-produce")
        return [max(0.0, float(value)) for value in solution]

    def iterate(self, final_demand: Sequence[float], *, max_iterations: int = 10_000,
                tolerance: float = 1e-12) -> dict:
        """Power-series iteration ``x_{k+1} = A x_k + y`` from ``x_0 = y``.

        Converges geometrically for a productive ``A``; a non-productive technology returns
        ``converged = False`` instead of silently returning a divergent vector.
        """
        demand = _as_vector(final_demand, self.size, "final_demand", non_negative=False,
                            tolerance=self.tolerance)
        A = np.asarray(self.A, dtype=float)
        current = demand.copy()
        residual = float("inf")
        iterations = 0
        for step in range(1, int(max_iterations) + 1):
            nxt = A @ current + demand
            residual = float(np.max(np.abs(nxt - current)))
            current = nxt
            iterations = step
            scale = max(1.0, float(np.max(np.abs(current))))
            if residual <= float(tolerance) * scale:
                return {"x": current.tolist(), "iterations": iterations, "converged": True,
                        "max_abs_change": residual}
        return {"x": current.tolist(), "iterations": iterations, "converged": False,
                "max_abs_change": residual}

    # -- derived quantities -------------------------------------------------------------
    def output_multipliers(self) -> list[float]:
        """Column sums of ``(I-A)^-1``: total output per unit of a sector's final demand."""
        inverse = np.asarray(self.inverse(), dtype=float)
        return inverse.sum(axis=0).tolist()

    def backward_linkages(self) -> list[float]:
        return self.output_multipliers()

    def forward_linkages(self) -> list[float]:
        """Row sums of ``(I-A)^-1``: output enabled by one unit of a sector's output."""
        return np.asarray(self.inverse(), dtype=float).sum(axis=1).tolist()

    def value_added(self, gross_output: Sequence[float]) -> list[float]:
        """``x - diag(x) A^T 1`` by industry: gross output minus its intermediate inputs.

        ``A[i][j]`` is input ``i`` per unit of output ``j``, so the intermediate inputs of
        industry ``j`` are ``sum_i A[i][j] x_j = x_j * (column sum j)``; the value added is the
        column-side balance of the same flow table (not a labour-content measure).
        """
        output = _as_vector(gross_output, self.size, "gross_output", non_negative=False,
                            tolerance=self.tolerance)
        column_sums = np.asarray(self.A, dtype=float).sum(axis=0)
        added = output - column_sums * output
        if np.any(added < -self.tolerance * max(1.0, float(np.max(np.abs(output))))):
            raise IOCoreError("negative value added: intermediate inputs exceed gross output")
        return [max(0.0, float(value)) for value in added]

    def material_residual(self, gross_output: Sequence[float],
                          final_demand: Sequence[float]) -> list[float]:
        """Componentwise ``A x + y - x``; zero means ``EQ-IO-001`` holds."""
        output = _as_vector(gross_output, self.size, "gross_output", non_negative=False,
                            tolerance=self.tolerance)
        demand = _as_vector(final_demand, self.size, "final_demand", non_negative=False,
                            tolerance=self.tolerance)
        return (np.asarray(self.A, dtype=float) @ output + demand - output).tolist()

    def describe(self) -> dict:
        """Metadata-only summary: no coefficient matrix is returned."""
        hawkins = self.hawkins_simon()
        return {"units": self.units, "sectors": self.size,
                "spectral_radius": round(self.spectral_radius(), 12),
                "productive": self.is_productive(),
                "productiveness_margin": round(self.productiveness_margin(), 12),
                "hawkins_simon_pass": hawkins["pass"],
                "min_leading_principal_minor": (
                    None if hawkins["leading_principal_minors"] is None
                    else round(min(hawkins["leading_principal_minors"]), 12)),
                "non_negative": True}


# ---------------------------------------------------------------------------------- scenarios
def final_demand_shock(dataset: IODataset, delta_y: Sequence[float], *, label: str = "") -> dict:
    """Shock to final demand: ``x' = (I - A)^-1 (y + delta_y)`` with the baseline technology.

    Returns the baseline and scenario aggregates plus the sector-level gross-output change, all
    in the dataset's value base.  A shock that drives final demand or gross output negative is
    refused (fail-closed).
    """
    if not isinstance(dataset, IODataset):
        raise IOCoreError("dataset must be an IODataset")
    core = dataset.leontief()
    delta = _as_vector(delta_y, dataset.size, "delta_y", non_negative=False,
                       tolerance=dataset.tolerance)
    baseline_y = dataset._array("y")
    new_y = baseline_y + delta
    if np.any(new_y < -dataset.tolerance):
        raise IOCoreError("final demand becomes negative under the shock")
    new_x = np.asarray(core.gross_output(new_y.tolist()), dtype=float)
    baseline_x = dataset._array("x")
    change = new_x - baseline_x
    total_change_y = float(delta.sum())
    total_change_x = float(change.sum())
    return {
        "scenario": "final_demand_shock" + (f":{label}" if label else ""),
        "units": dataset.units,
        "baseline": {"total_output": float(baseline_x.sum()),
                     "total_final_demand": float(baseline_y.sum()),
                     "spectral_radius": core.spectral_radius(),
                     "productive": core.is_productive()},
        "shock": {"delta_final_demand": delta.tolist(),
                  "total_delta_final_demand": total_change_y,
                  "output_multipliers": core.output_multipliers()},
        "result": {"final_demand": new_y.tolist(), "gross_output": new_x.tolist(),
                   "delta_gross_output": change.tolist(),
                   "total_gross_output": float(new_x.sum()),
                   "total_delta_gross_output": total_change_x,
                   "output_multiplier": (total_change_x / total_change_y
                                         if abs(total_change_y) > dataset.tolerance else None)},
        "balances": {"material_residual_max_abs": float(np.max(np.abs(
            core.material_residual(new_x.tolist(), new_y.tolist()))))},
        "feasible": True,
    }


def technology_shock(dataset: IODataset, delta_A: Sequence[Sequence[float]],
                     delta_y: Sequence[float] | None = None, *, label: str = "") -> dict:
    """Technology shock: ``A' = A + delta_A``, then ``x' = (I - A')^-1 (y + delta_y)``.

    The shocked technology must stay non-negative and productive; otherwise the scenario is
    refused instead of returning negative gross output.
    """
    if not isinstance(dataset, IODataset):
        raise IOCoreError("dataset must be an IODataset")
    base = dataset.leontief()
    delta_matrix = _as_matrix(delta_A, "delta_A", non_negative=False)
    if delta_matrix.shape != (dataset.size, dataset.size):
        raise IOCoreError(f"delta_A must be {dataset.size} x {dataset.size}, got {delta_matrix.shape}")
    A_new = np.asarray(base.A, dtype=float) + delta_matrix
    if np.any(A_new < -dataset.tolerance):
        raise IOCoreError("technology shock makes a coefficient negative")
    shocked = LeontiefCore(tuple(tuple(row) for row in A_new), dataset.labels,
                           units=dataset.units, tolerance=dataset.tolerance)
    delta = (np.zeros(dataset.size) if delta_y is None
             else _as_vector(delta_y, dataset.size, "delta_y", non_negative=False,
                             tolerance=dataset.tolerance))
    new_y = dataset._array("y") + delta
    if np.any(new_y < -dataset.tolerance):
        raise IOCoreError("final demand becomes negative under the technology shock")
    new_x = np.asarray(shocked.gross_output(new_y.tolist()), dtype=float)
    baseline_x = dataset._array("x")
    return {
        "scenario": "technology_shock" + (f":{label}" if label else ""),
        "units": dataset.units,
        "baseline": {"total_output": float(baseline_x.sum()),
                     "spectral_radius": base.spectral_radius(),
                     "productive": base.is_productive()},
        "technology": {"delta_A": delta_matrix.tolist(),
                       "max_abs_delta_A": float(np.max(np.abs(delta_matrix))),
                       "spectral_radius_after": shocked.spectral_radius(),
                       "productive_after": shocked.is_productive(),
                       "hawkins_simon_after": shocked.hawkins_simon()["pass"]},
        "result": {"gross_output": new_x.tolist(),
                   "delta_gross_output": (new_x - baseline_x).tolist(),
                   "total_gross_output": float(new_x.sum()),
                   "total_delta_gross_output": float((new_x - baseline_x).sum())},
        "feasible": True,
    }


__all__ = [
    "DEFAULT_UNITS", "IOCoreError", "IODataset", "LEVELS", "LeontiefCore", "TOLERANCE",
    "aggregate_matrix_by_country", "aggregate_matrix_by_industry", "aggregate_vector_by_country",
    "aggregate_vector_by_industry", "check_dataset", "final_demand_shock", "technology_shock",
]
