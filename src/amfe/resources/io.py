"""Leontief input-output model: productiveness, inverse and gross output (docs/38 I01).

Balance contract
----------------
The direct-requirements matrix ``A`` (``a[i][j]`` units of product ``i`` per unit of gross
output of sector ``j``) is non-negative in physical or one consistent value base.  The
material balance of gross output is the identity

    x = A x + d,        d >= 0 is final demand,

so ``x = (I - A)^-1 d`` **only if the inverse exists**, and an economically meaningful
non-negative solution exists only when ``A`` is productive (``rho(A) < 1``, the
Perron-Frobenius radius; equivalently ``(I - A)^-1 = sum_k A^k >= 0`` and convergent).
The prototype refuses to solve a singular or non-productive system instead of returning
negative gross output: productiveness is *checked*, not assumed (docs/42 stage 4).

What this class deliberately does not do
----------------------------------------
* It does not claim the coefficients are constant over time: one instance is one
  technology snapshot (docs/38 E07), and comparing snapshots is the caller's job.
* It does not evaluate anything in money, does not set prices and contains no labour
  theory of value: ``value_added_j = x_j - sum_i A[i][j] x_j`` is the accounting residual
  of the balance, not a labour-content measure (docs/35, docs/38 E01/E03).
* It does not treat intermediate and final output as addable: gross output, intermediate
  use and value added are separate vectors, and only value added equals final uses for a
  closed economy (see :meth:`total_value_added` and :mod:`amfe.resources.balances`).

Public API
----------
``LeontiefModel``    validated matrix, inverse, gross output, value added, multipliers.
``LeontiefError``    invalid data or a non-invertible / non-productive model.
``solve_linear``     dense linear solve used for the inverse (stdlib only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Sequence, Tuple

TOLERANCE = 1e-9


class LeontiefError(ValueError):
    """Invalid input-output data or a system that cannot be solved productively."""


# --------------------------------------------------------------------------------------
# Dense linear algebra (standard library only: the CI runner does not install numpy)
# --------------------------------------------------------------------------------------
def _as_square(matrix: Sequence[Sequence[float]], name: str = "matrix") -> List[List[float]]:
    rows = [list(row) for row in matrix]
    if not rows:
        raise LeontiefError(f"{name} must not be empty")
    size = len(rows)
    for index, row in enumerate(rows):
        if len(row) != size:
            raise LeontiefError(f"{name} must be square: row {index} has {len(row)} entries")
    return rows


def _validate_finite(values: Iterable[float], name: str) -> None:
    for index, value in enumerate(values):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise LeontiefError(f"{name}[{index}] is not a number: {value!r}") from exc
        if not math.isfinite(number):
            raise LeontiefError(f"{name}[{index}] must be finite, got {number!r}")


def solve_linear(matrix: Sequence[Sequence[float]], rhs: Sequence[float]) -> List[float]:
    """Solve ``matrix @ x = rhs`` with partial-pivot Gauss-Jordan elimination.

    Values are compared to ``TOLERANCE`` relative to the pivot magnitude; a singular
    matrix raises :class:`LeontiefError` instead of returning infinities.
    """
    coefficients = _as_square(matrix, "matrix")
    size = len(coefficients)
    if len(rhs) != size:
        raise LeontiefError(f"rhs must have {size} entries, got {len(rhs)}")
    _validate_finite(rhs, "rhs")
    augmented = [row + [float(rhs[i])] for i, row in enumerate(coefficients)]
    _validate_finite([value for row in augmented for value in row], "matrix")

    for column in range(size):
        pivot_row = max(range(column, size), key=lambda r: abs(augmented[r][column]))
        pivot = augmented[pivot_row][column]
        if abs(pivot) <= TOLERANCE:
            raise LeontiefError(
                f"matrix is singular (zero pivot at column {column}); the inverse of "
                "(I - A) does not exist"
            )
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]
        pivot = augmented[column][column]
        augmented[column] = [value / pivot for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def inverse_matrix(matrix: Sequence[Sequence[float]]) -> List[List[float]]:
    """Invert a dense matrix by solving one unit-vector system per column."""
    coefficients = _as_square(matrix, "matrix")
    size = len(coefficients)
    columns = []
    for column in range(size):
        unit = [1.0 if row == column else 0.0 for row in range(size)]
        columns.append(solve_linear(coefficients, unit))
    return [[columns[column][row] for column in range(size)] for row in range(size)]


def matvec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> List[float]:
    """Dense matrix-vector product with an explicit shape check."""
    _validate_finite(vector, "vector")
    matrix_rows = [list(row) for row in matrix]
    for index, row in enumerate(matrix_rows):
        if len(row) != len(vector):
            raise LeontiefError(
                f"matrix row {index} has {len(row)} columns, vector has {len(vector)}"
            )
    return [sum(row[j] * vector[j] for j in range(len(vector))) for row in matrix_rows]


def spectral_radius(matrix: Sequence[Sequence[float]], max_iterations: int = 10_000,
                    tolerance: float = 1e-13) -> float:
    """Estimate the Perron-Frobenius radius of a non-negative matrix by power iteration.

    For a non-negative matrix the iteration ``v <- A v / max(A v)`` converges (in norm
    and in the dominant eigenvalue estimate) whenever the spectral radius is positive;
    periodic matrices such as a permutation matrix keep the estimate at 1, which is the
    correct radius.  A zero matrix returns 0.  The result is an estimate with the
    documented stopping tolerance, which is enough to decide ``rho < 1`` away from the
    boundary; the boundary itself is treated as non-productive.
    """
    coefficients = _as_square(matrix, "matrix")
    size = len(coefficients)
    _validate_finite([value for row in coefficients for value in row], "matrix")
    for row in coefficients:
        for value in row:
            if value < 0.0:
                raise LeontiefError("spectral radius of a non-negative matrix requires A >= 0")
    vector = [1.0 / size] * size
    estimate = 0.0
    for _ in range(max_iterations):
        product = matvec(coefficients, vector)
        norm = max(abs(value) for value in product)
        if norm == 0.0:
            return 0.0
        estimate = norm
        if max(abs(product[i] / norm - vector[i]) for i in range(size)) <= tolerance:
            return estimate
        vector = [value / norm for value in product]
    return estimate


def _vector(values: Iterable[float], size: int, name: str, non_negative: bool = True) -> List[float]:
    vector = [float(value) for value in values]
    if len(vector) != size:
        raise LeontiefError(f"{name} must have {size} entries, got {len(vector)}")
    _validate_finite(vector, name)
    if non_negative and any(value < -TOLERANCE for value in vector):
        raise LeontiefError(f"{name} must be non-negative, got {vector}")
    return vector


@dataclass
class LeontiefModel:
    """A validated direct-requirements matrix ``A`` with sector labels.

    ``A[i][j]`` is the amount of product ``i`` needed per unit of gross output of sector
    ``j``.  ``sector_ids`` names both rows and columns (one product per sector, the
    prototype restriction; a product-by-industry extension needs a use table and raises
    that restriction explicitly through :meth:`from_use_table`).
    """

    A: Tuple[Tuple[float, ...], ...]
    sector_ids: Tuple[str, ...]
    tolerance: float = TOLERANCE

    def __post_init__(self) -> None:
        matrix = _as_square([list(row) for row in self.A], "A")
        sectors = tuple(str(name) for name in self.sector_ids)
        if len(sectors) != len(matrix):
            raise LeontiefError("sector_ids must have one label per matrix row")
        if len(set(sectors)) != len(sectors):
            raise LeontiefError("sector_ids must be unique")
        for row in matrix:
            for value in row:
                if value < -self.tolerance:
                    raise LeontiefError("A must be non-negative (E03: no monetary sign tricks)")
        object.__setattr__(self, "A", tuple(tuple(float(v) for v in row) for row in matrix))
        object.__setattr__(self, "sector_ids", sectors)
        if self.tolerance <= 0.0:
            raise LeontiefError("tolerance must be positive")

    # -- construction ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self.sector_ids)

    def index(self, sector: str) -> int:
        try:
            return self.sector_ids.index(sector)
        except ValueError as exc:
            raise LeontiefError(f"unknown sector {sector!r}") from exc

    @classmethod
    def uniform(cls, sector_ids: Sequence[str], coefficient: float) -> "LeontiefModel":
        """A model where every sector uses ``coefficient`` of every product per unit."""
        sectors = tuple(str(name) for name in sector_ids)
        matrix = tuple(tuple(float(coefficient) for _ in sectors) for _ in sectors)
        return cls(matrix, sectors)

    @classmethod
    def from_requirements(cls, rows: Mapping[str, Mapping[str, float]]) -> "LeontiefModel":
        """Build from ``{using_sector: {producing_sector: coefficient}}``."""
        sectors = tuple(rows.keys())
        matrix = []
        for using in sectors:
            row = rows[using]
            if set(row.keys()) != set(sectors):
                raise LeontiefError(
                    f"row {using!r} must list exactly the sectors {list(sectors)}"
                )
            matrix.append(tuple(float(row[producing]) for producing in sectors))
        return cls(tuple(matrix), sectors)

    # -- properties --------------------------------------------------------------------
    def technical_matrix(self) -> List[List[float]]:
        """``A`` as a mutable list of lists (a copy)."""
        return [list(row) for row in self.A]

    def leontief_matrix(self) -> List[List[float]]:
        identity_minus_a = [[-value for value in row] for row in self.A]
        for index in range(self.size):
            identity_minus_a[index][index] += 1.0
        return identity_minus_a

    def spectral_radius(self) -> float:
        """Estimated Perron-Frobenius radius of ``A`` (see :func:`spectral_radius`)."""
        return spectral_radius(self.A)

    def is_productive(self) -> bool:
        """``True`` iff ``rho(A) < 1`` within tolerance: ``(I-A)^-1`` exists and is non-negative."""
        radius = self.spectral_radius()
        return radius < 1.0 - self.tolerance

    def productiveness_margin(self) -> float:
        """``1 - rho(A)``: positive for a productive technology, ~0 at the boundary."""
        return 1.0 - self.spectral_radius()

    def inverse(self) -> List[List[float]]:
        """``(I - A)^-1``; raises :class:`LeontiefError` when singular or negative."""
        inverse = inverse_matrix(self.leontief_matrix())
        if any(value < -self.tolerance for row in inverse for value in row):
            raise LeontiefError(
                "inverse of (I - A) has negative entries: A is not productive "
                f"(rho >= 1, rho ~ {self.spectral_radius():.6f})"
            )
        return inverse

    # -- output ------------------------------------------------------------------------
    def gross_output(self, final_demand: Sequence[float]) -> List[float]:
        """``x = (I - A)^-1 d`` for non-negative final demand ``d``.

        The model is required to be productive; a singular or non-productive matrix raises
        :class:`LeontiefError` (no negative gross output is ever returned).
        """
        demand = _vector(final_demand, self.size, "final_demand", non_negative=True)
        if not self.is_productive():
            raise LeontiefError(
                "A is not productive (rho(A) >= 1): the material balance has no non-negative "
                "gross output for positive final demand"
            )
        output = matvec(self.inverse(), demand)
        if any(value < -self.tolerance for value in output):
            raise LeontiefError(f"negative gross output {output}: A is not productive")
        return [max(0.0, value) for value in output]

    def intermediate_use(self, gross_output: Sequence[float]) -> List[float]:
        """``A x``: intermediate deliveries by sector (not addable to gross output, E01)."""
        output = _vector(gross_output, self.size, "gross_output", non_negative=True)
        return matvec(self.A, output)

    def value_added(self, gross_output: Sequence[float]) -> List[float]:
        """``x - A x`` by sector: the accounting residual of the balance."""
        output = _vector(gross_output, self.size, "gross_output", non_negative=True)
        intermediate = matvec(self.A, output)
        added = [output[i] - intermediate[i] for i in range(self.size)]
        if any(value < -self.tolerance for value in added):
            raise LeontiefError(
                "negative value added: gross output is smaller than intermediate use, the "
                "balance is inconsistent"
            )
        return [max(0.0, value) for value in added]

    def total_value_added(self, gross_output: Sequence[float]) -> float:
        return math.fsum(self.value_added(gross_output))

    def final_demand_from_output(self, gross_output: Sequence[float]) -> List[float]:
        """``d = (I - A) x``: final uses consistent with a gross-output vector."""
        output = _vector(gross_output, self.size, "gross_output", non_negative=True)
        intermediate = matvec(self.A, output)
        demand = [output[i] - intermediate[i] for i in range(self.size)]
        if any(value < -self.tolerance for value in demand):
            raise LeontiefError("negative final demand: A x exceeds x, the balance is inconsistent")
        return [max(0.0, value) for value in demand]

    def material_residual(self, gross_output: Sequence[float],
                          final_demand: Sequence[float]) -> List[float]:
        """Componentwise ``A x + d - x``; zero means the balance holds (I01)."""
        output = _vector(gross_output, self.size, "gross_output")
        demand = _vector(final_demand, self.size, "final_demand")
        intermediate = matvec(self.A, output)
        return [intermediate[i] + demand[i] - output[i] for i in range(self.size)]

    def check_material_balance(self, gross_output: Sequence[float],
                               final_demand: Sequence[float],
                               tolerance: float = TOLERANCE) -> float:
        """Raise unless ``x = A x + d``; returns the largest absolute residual."""
        residual = self.material_residual(gross_output, final_demand)
        largest = max(abs(value) for value in residual) if residual else 0.0
        if largest > tolerance:
            raise LeontiefError(
                f"material balance violated: max |A x + d - x| = {largest:.3e}"
            )
        return largest

    def output_multipliers(self) -> List[float]:
        """Column sums of ``(I-A)^-1``: total gross output per unit of final demand."""
        inverse = self.inverse()
        return [math.fsum(inverse[row][column] for row in range(self.size))
                for column in range(self.size)]

    def backward_linkages(self) -> List[float]:
        return self.output_multipliers()

    def forward_linkages(self) -> List[float]:
        """Row sums of ``(I-A)^-1``: total output enabled by one unit of output of a sector."""
        inverse = self.inverse()
        return [math.fsum(row) for row in inverse]

    def describe(self) -> dict:
        """Metadata-only summary (no raw matrix in reports, mirrors the data-gate style)."""
        return {
            "sectors": list(self.sector_ids),
            "size": self.size,
            "non_negative": True,
            "spectral_radius": round(self.spectral_radius(), 12),
            "productive": self.is_productive(),
            "productiveness_margin": round(self.productiveness_margin(), 12),
        }
