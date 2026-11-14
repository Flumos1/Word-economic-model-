"""P2 world pipeline: data -> static core -> scenarios -> report (docs/127).

The pipeline is the *performance* layer over the already-validated cores:

* canonical data --``amfe.mrio.loaders`` (fail-closed local loader);
* static core   --``amfe.world.io_core`` (``A = Z/x``, Leontief, rho(A), Hawkins-Simon,
  country/industry aggregation, demand and technology shocks) on the **country block**, where the
  dense reference algorithms are cheap (``n_countries x n_countries``);
* sector core   -- sparse ``scipy.sparse`` algebra on the full ``n_c * n_i`` system (2700+ sectors):
  one ``splu`` factorisation of ``I - A`` reused by every solve, sparse Perron root, sparse
  scenario shocks;
* SFC check     --``amfe.world.sfc_core`` verifies the FX revaluation bookkeeping of a scenario.

Design rules (docs/127):

* **Fail-closed.**  A missing/invalid dataset, a non-productive technology, a negative demand, an
  infeasible shock or a failed balance raises :class:`PipelineError` (exit 1) or ``BLOCKED_DATA``
  (exit 2).  No partial report is written on failure.
* **Deterministic.**  Labels are sorted, synthetic blocks are seeded from a stable SHA-256 of
  ``(seed, country)``, chunk boundaries are per-country (independent of the worker count) and all
  merges happen in sorted order, so the report is byte-identical for ``--workers 1`` and
  ``--workers 8`` (except the timings block, see :func:`strip_timings`).
* **Aggregate-only output.**  The report contains totals, timings, checksums and per-country /
  per-industry aggregates; never the raw ``Z`` matrix or per-sector forecasts (same rule as
  docs/121 for the MRIO data track).
* **Bounded memory.**  ``csr`` storage, a single sparse factorisation, and streaming per-country
  chunks keep the 60x45 benchmark well under the 2 GB budget; :func:`peak_memory_mb` reports the
  process peak.

CLI
---
    python tools/world_pipeline.py --dataset figaro --build --solve --scenario energy
    python tools/world_pipeline.py --synthetic --countries 60 --industries 45 --benchmark
    python tools/world_pipeline.py --dataset data/mrio/figaro/dataset.json --build --out build/world
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tracemalloc
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.mrio.loaders import (  # noqa: E402
    DATA_ROOT_ENV,
    DATASET_FILENAME,
    LoadedDataset,
    available_datasets,
    load_dataset,
    mrio_root,
    sha256_file,
)
from amfe.mrio.schema import (  # noqa: E402
    MIN_COUNTRIES,
    MIN_INDUSTRIES,
    MRIODataset,
    MRIOError,
)
from amfe.world.io_core import (  # noqa: E402
    IOCoreError,
    IODataset,
    LeontiefCore,
    aggregate_matrix_by_country,
    final_demand_shock,
)
from amfe.world.sfc_core import SFCFrame, SFCError  # noqa: E402

PIPELINE_VERSION = "1.0.0"
DEFAULT_OUT = ROOT / "build" / "world"
STATUS_OK = "OK"
STATUS_INVALID = "INVALID"
STATUS_BLOCKED = "BLOCKED_DATA"
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
#: Dense canonical conversion guard: above this many sectors a list-of-lists document is refused.
DENSE_LIMIT = 4096
#: Perron-root power iteration defaults (non-negative A, so the iteration converges to rho(A)).
POWER_ITERATIONS = 5000
POWER_TOLERANCE = 1e-13
#: Sector-name keywords used by the energy scenario when no explicit sector list is given.
ENERGY_KEYWORDS = ("energy", "coal", "oil", "gas", "electric", "petro", "coke", "nuclear")
UNITS = "USD_mn"


class PipelineError(RuntimeError):
    """A pipeline stage refused to continue (fail-closed)."""


class BlockedData(PipelineError):
    """No usable local dataset (maps to ``BLOCKED_DATA`` / exit 2)."""


# -------------------------------------------------------------------------------------- timings
@dataclass
class Timings:
    """Named wall-clock stage durations in milliseconds (rounded for a stable report)."""

    stages: dict = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round((time.perf_counter() - started) * 1000.0, 3)

    def add(self, name: str, milliseconds: float) -> None:
        self.stages[name] = round(float(milliseconds), 3)

    def as_dict(self) -> dict:
        return dict(sorted(self.stages.items()))


def strip_timings(report: dict) -> dict:
    """Return a copy of ``report`` without the volatile blocks (used to prove determinism).

    Dropped: timings, peak memory, wall time and the worker count itself (the whole point is that
    the numerical report is identical for ``--workers 1`` and ``--workers 8``).
    """
    clean = {key: value for key, value in report.items()
             if key not in ("timings_ms", "peak_memory_mb", "elapsed_ms", "workers")}
    return json.loads(json.dumps(clean, sort_keys=True))


def peak_memory_mb() -> float:
    """Peak resident memory of this process in MB (psutil peak_wset, else current RSS)."""
    try:
        import psutil  # local import: optional dependency

        info = psutil.Process(os.getpid()).memory_info()
        peak = getattr(info, "peak_wset", None) or getattr(info, "rss", 0)
        return round(float(peak) / (1024.0 * 1024.0), 1)
    except Exception:  # pragma: no cover - defensive
        current, peak = tracemalloc.get_traced_memory()
        return round(float(max(current, peak)) / (1024.0 * 1024.0), 1)


def _stable_seed(seed: int, *parts) -> int:
    """Worker-independent seed: SHA-256 of the parts, so parallel == serial bit for bit."""
    text = ":".join([str(int(seed)), *[str(part) for part in parts]])
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


# ----------------------------------------------------------------------------------- world table
@dataclass
class WorldTable:
    """A validated MRIO snapshot held sparsely: ``Z`` (n x n), ``y``, ``x``, ``va`` (n)."""

    countries: tuple[str, ...]
    industries: tuple[str, ...]
    Z: sp.csr_matrix
    y: np.ndarray
    x: np.ndarray
    va: np.ndarray
    units: str = UNITS
    tolerance: float = 1e-6
    provenance: dict = field(default_factory=dict)

    # -- basics ---------------------------------------------------------------------------
    @property
    def n_countries(self) -> int:
        return len(self.countries)

    @property
    def n_industries(self) -> int:
        return len(self.industries)

    @property
    def n(self) -> int:
        return self.n_countries * self.n_industries

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(f"{country}|{industry}" for country in self.countries
                     for industry in self.industries)

    def country_of(self, index: int) -> str:
        return self.countries[index // self.n_industries]

    def industry_of(self, index: int) -> str:
        return self.industries[index % self.n_industries]

    def index_of(self, country: str, industry: str) -> int:
        try:
            return self.countries.index(country) * self.n_industries + self.industries.index(industry)
        except ValueError as exc:
            raise PipelineError(f"unknown (country, industry) pair: ({country!r}, {industry!r})") from exc

    def country_slice(self, country: str) -> slice:
        if country not in self.countries:
            raise PipelineError(f"unknown country {country!r}")
        start = self.countries.index(country) * self.n_industries
        return slice(start, start + self.n_industries)

    def technical_sparse(self) -> sp.csr_matrix:
        """``A = Z diag(x)^-1`` as CSR; zero-output sectors get zero columns (fail-closed negative)."""
        if np.any(self.x < -self.tolerance):
            raise PipelineError("gross output x must be non-negative for A = Z / x")
        inverse = np.zeros_like(self.x, dtype=float)
        positive = self.x > 0.0
        inverse[positive] = 1.0 / self.x[positive]
        A = (self.Z @ sp.diags(inverse)).tocsr()
        A.sum_duplicates()
        A.sort_indices()
        if A.nnz and float(A.data.min()) < -self.tolerance:
            raise PipelineError("technical coefficients are negative")
        return A

    def country_block(self) -> tuple[sp.csr_matrix, np.ndarray, np.ndarray, np.ndarray]:
        """Aggregate to a country x country flow table (``Z_c = P Z P'``)."""
        projection = _country_projection(self.n_countries, self.n_industries)
        Zc = (projection @ self.Z @ projection.T).tocsr()
        Zc.sum_duplicates()
        Zc.sort_indices()
        yc = np.asarray(projection @ self.y).ravel()
        xc = np.asarray(projection @ self.x).ravel()
        vac = np.asarray(projection @ self.va).ravel()
        return Zc, yc, xc, vac

    def to_iodataset(self, level: str = "country") -> IODataset:
        """Dense ``amfe.world.io_core.IODataset`` at country (or industry) level."""
        if level == "country":
            Zc, yc, xc, vac = self.country_block()
            return IODataset(countries=self.countries, industries=("TOTAL",),
                             Z=_dense(Zc), y=yc.tolist(), x=xc.tolist(), va=vac.tolist(),
                             units=self.units, tolerance=self.tolerance)
        projection = _industry_projection(self.n_countries, self.n_industries)
        Zi = (projection @ self.Z @ projection.T).tocsr()
        return IODataset(countries=("WORLD",), industries=self.industries,
                         Z=_dense(Zi), y=np.asarray(projection @ self.y).ravel().tolist(),
                         x=np.asarray(projection @ self.x).ravel().tolist(),
                         va=np.asarray(projection @ self.va).ravel().tolist(),
                         units=self.units, tolerance=self.tolerance)

    def describe(self) -> dict:
        """Metadata-only summary (no observation values)."""
        return {
            "units": self.units,
            "countries": self.n_countries,
            "industries": self.n_industries,
            "sectors": self.n,
            "cells_Z": self.n * self.n,
            "stored_Z": int(self.Z.nnz),
            "stored_share": round(float(self.Z.nnz) / float(self.n * self.n), 6),
            "total_output": round(float(self.x.sum()), 6),
            "total_final_demand": round(float(self.y.sum()), 6),
            "total_value_added": round(float(self.va.sum()), 6),
            "provenance": dict(sorted(self.provenance.items())),
        }

    def to_canonical_dense(self, limit: int = DENSE_LIMIT) -> MRIODataset:
        """Canonical ``MRIODataset`` (dense tuples); refused above ``limit`` sectors."""
        if self.n > limit:
            raise PipelineError(
                f"refusing a dense canonical document for {self.n} sectors (> DENSE_LIMIT={limit}): "
                "the canonical schema is a list-of-lists JSON and would defeat the memory budget")
        Z = _dense(self.Z)
        return MRIODataset(countries=self.countries, industries=self.industries,
                           years=tuple(int(year) for year in self.provenance.get("years", [2020])),
                           Z=Z, y=self.y.tolist(), x=self.x.tolist(), va=self.va.tolist(),
                           units=self.units, tolerance=self.tolerance,
                           source={k: v for k, v in self.provenance.items()
                                   if k in ("name", "note", "synthetic", "seed")})


def _dense(matrix) -> list[list[float]]:
    return np.asarray(matrix.todense(), dtype=float).tolist()


def _country_projection(n_countries: int, n_industries: int) -> sp.csr_matrix:
    rows = np.arange(n_countries * n_industries) // n_industries
    cols = np.arange(n_countries * n_industries)
    return sp.csr_matrix((np.ones(cols.size), (rows, cols)), shape=(n_countries, cols.size))


def _industry_projection(n_countries: int, n_industries: int) -> sp.csr_matrix:
    rows = np.arange(n_countries * n_industries) % n_industries
    cols = np.arange(n_countries * n_industries)
    return sp.csr_matrix((np.ones(cols.size), (rows, cols)), shape=(n_industries, cols.size))


def _chunk_tasks(n_countries: int, n_industries: int, size: int):
    """Yield ``(country_index, start, stop)`` chunks; boundaries independent of the worker count."""
    for country_index in range(n_countries):
        start = country_index * n_industries
        yield country_index, start, start + n_industries


def _run_chunks(function, tasks, workers: int):
    """Map ``function`` over per-country tasks, in order, with or without a process pool."""
    tasks = list(tasks)
    workers = max(1, int(workers))
    if workers == 1 or len(tasks) <= 1:
        return [function(task) for task in tasks]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, tasks))


def _workers(requested, cap: int = 8) -> int:
    if requested is not None:
        workers = int(requested)
        if workers < 1:
            raise PipelineError("--workers must be a positive integer")
        return min(workers, cap)
    return max(1, min(int(os.cpu_count() or 1), cap))


# --------------------------------------------------------------------------- parallel build stages
def _build_country_block(task):
    """Worker: synthetic random input block for one origin country (rows = its 45 sectors)."""
    country_index, n_countries, n_industries, seed, foreign_entries = task
    n = n_countries * n_industries
    ni = n_industries
    rng = np.random.default_rng(_stable_seed(seed, "block", country_index))
    row_local = np.repeat(np.arange(ni), ni + foreign_entries)
    row_global = country_index * ni + row_local
    # domestic block: every industry of the same country
    cols_domestic = (country_index * ni + np.tile(np.arange(ni), ni)).reshape(ni, ni)
    # foreign inputs: `foreign_entries` per row, drawn from the other countries
    foreign = rng.integers(0, n - ni, size=(ni, foreign_entries))
    foreign = np.where(foreign >= country_index * ni, foreign + ni, foreign)
    cols = np.concatenate([cols_domestic, foreign], axis=1).ravel()
    data = rng.random(row_local.size)
    scale = np.where(np.tile(np.arange(ni + foreign_entries) < ni, ni), 1.0, 0.25)
    return row_global, cols, data * scale


def synthetic_table(n_countries: int = 60, n_industries: int = 45, seed: int = 20261114,
                    foreign_entries: int = 8, workers: int = 1,
                    timings: Timings | None = None) -> WorldTable:
    """Deterministic synthetic MRIO table (60 x 45 by default) with exact IO balances.

    The table is a random sparse Leontief economy: ``A = W diag(s) / colsum(W)`` (column
    intermediate shares ``s`` in [0.38, 0.52], so ``rho(A) <= max(s) < 1``), final demand ``y0``
    drawn uniformly, and ``x = (I - A)^-1 y0`` solved sparsely.  ``Z = A diag(x)``, ``va = x - Z'1``,
    so both accounting identities hold by construction.
    """
    if n_countries < 1 or n_industries < 1:
        raise PipelineError("countries and industries must be positive")
    timings = timings or Timings()
    countries = tuple(f"C{index + 1:02d}" for index in range(n_countries))
    industries = tuple(f"S{index + 1:02d}" for index in range(n_industries))
    n = n_countries * n_industries
    with timings.stage("synthetic_blocks"):
        tasks = [(index, n_countries, n_industries, seed, foreign_entries)
                 for index in range(n_countries)]
        blocks = _run_chunks(_build_country_block, tasks, workers)
    with timings.stage("synthetic_assemble"):
        rows = np.concatenate([block[0] for block in blocks])
        cols = np.concatenate([block[1] for block in blocks])
        data = np.concatenate([block[2] for block in blocks])
        W = sp.coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()
        W.sum_duplicates()
        W.sort_indices()
        rng = np.random.default_rng(_stable_seed(seed, "columns"))
        shares = rng.uniform(0.38, 0.52, size=n)
        column_sums = np.asarray(W.sum(axis=0)).ravel()
        column_sums[column_sums <= 0.0] = 1.0
        A = (W @ sp.diags(shares / column_sums)).tocsr()
        A.sum_duplicates()
        A.sort_indices()
        demand_rng = np.random.default_rng(_stable_seed(seed, "demand"))
        y = demand_rng.uniform(500.0, 1500.0, size=n)
    with timings.stage("synthetic_solve"):
        # Power-series iteration x = A x + y converges geometrically for rho(A) <= 0.52; it is
        # ~100x cheaper than a factorisation and reaches the same fixed point.
        x = y.copy()
        iterations = 0
        converged = False
        for iterations in range(1, 10_000):
            nxt = A @ x + y
            change = float(np.max(np.abs(nxt - x)))
            x = nxt
            if change <= 1e-13 * max(1.0, float(np.max(np.abs(x)))):
                converged = True
                break
        if not converged:
            raise PipelineError("synthetic table: power series did not converge (not productive)")
        if not np.all(np.isfinite(x)) or float(x.min()) <= 0.0:
            raise PipelineError("synthetic table is not productive: x must be finite and positive")
    with timings.stage("synthetic_materialise"):
        Z = (A @ sp.diags(x)).tocsr()
        Z.sum_duplicates()
        Z.sort_indices()
        va = x - np.asarray(Z.sum(axis=0)).ravel()
    table = WorldTable(countries=countries, industries=industries, Z=Z, y=y, x=x, va=va,
                       units=UNITS, tolerance=1e-6,
                       provenance={"name": "synthetic", "synthetic": True, "seed": int(seed),
                                   "foreign_entries": int(foreign_entries), "years": [2020],
                                   "note": "deterministic synthetic table; not an observation"})
    return table


def table_from_canonical(loaded: LoadedDataset) -> WorldTable:
    """Convert a canonical (dense) dataset into the sparse pipeline table.

    The canonical document keeps the source order of ``countries``/``industries``; the pipeline
    re-orders both to a sorted, deterministic order (one permutation applied to rows and columns
    alike), so reports and scenario aggregates never depend on the file's ordering.
    """
    dataset = loaded.dataset
    Z = sp.csr_matrix(np.asarray(dataset.Z, dtype=float))
    y = np.asarray(dataset.y, dtype=float)
    x = np.asarray(dataset.x, dtype=float)
    va = np.asarray(dataset.va, dtype=float)
    countries = tuple(sorted(dataset.countries))
    industries = tuple(sorted(dataset.industries))
    order = np.asarray(
        [country_index * len(dataset.industries) + industry_index
         for country_index in [dataset.countries.index(name) for name in countries]
         for industry_index in [dataset.industries.index(name) for name in industries]],
        dtype=np.int64)
    Z = Z[order][:, order].tocsr()
    Z.sum_duplicates()
    Z.sort_indices()
    return WorldTable(countries=countries, industries=industries,
                      Z=Z, y=y[order], x=x[order], va=va[order],
                      units=dataset.units, tolerance=float(dataset.tolerance),
                      provenance={"name": str(loaded.path.name), "dataset_sha256": loaded.sha256,
                                  "path": str(loaded.path), "years": list(dataset.years),
                                  "source": dataset.source.get("name") if dataset.source else None,
                                  "synthetic": False})


# ------------------------------------------------------------------------------- parallel checks
def drop_inactive_sectors(table: WorldTable) -> tuple[WorldTable, dict]:
    """Drop sectors with zero gross output before A = Z/x (real MRIO tables contain them).

    Real tables have sectors with no production in a given year (e.g. FIGARO ``CPA_U``); they carry
    zero rows/columns, so they cannot enter ``A = Z / x``.  The filter is explicit and reported: it
    never silently imputes output.  The kept subset is re-validated by the normal checks.
    """
    mask = table.x > 0.0
    dropped_count = int((~mask).sum())
    if dropped_count == 0:
        return table, {"dropped": 0, "kept": int(table.n), "examples": []}
    keep = np.flatnonzero(mask)
    n_industries = table.n_industries
    labels = [(table.countries[int(index) // n_industries],
               table.industries[int(index) % n_industries])
              for index in np.flatnonzero(~mask)]
    filtered = WorldTable(
        countries=table.countries, industries=table.industries,
        Z=table.Z[keep][:, keep].tocsr(), y=table.y[keep], x=table.x[keep], va=table.va[keep],
        units=table.units, tolerance=table.tolerance, provenance=dict(table.provenance))
    return filtered, {"dropped": dropped_count, "kept": int(filtered.n),
                      "examples": [f"{country}/{industry}" for country, industry in labels[:12]]}


def _country_balance_chunk(task):
    """Worker: row-side material balance for one country's rows."""
    Z_block, y_block, x_block = task
    row_sums = np.asarray(Z_block.sum(axis=1)).ravel()
    residual = row_sums + y_block - x_block
    return {"max_abs_row_residual": float(np.max(np.abs(residual))) if residual.size else 0.0,
            "min_Z": float(Z_block.data.min()) if Z_block.nnz else 0.0,
            "nnz": int(Z_block.nnz)}


def _country_aggregate_chunk(task):
    """Worker: country-block aggregation of one country's rows (``P Z P'`` row chunk)."""
    Z_block, n_countries, n_industries = task
    totals = np.asarray(Z_block.sum(axis=0)).ravel().reshape(n_countries, n_industries).sum(axis=1)
    return totals


def aggregate_country_parallel(table: WorldTable, workers: int = 1) -> sp.csr_matrix:
    """Country-block aggregation with one task per country (deterministic merge order)."""
    tasks = [table.Z[table.country_slice(country)] for country in table.countries]
    chunk_tasks = [(block, table.n_countries, table.n_industries) for block in tasks]
    results = _run_chunks(_country_aggregate_chunk, chunk_tasks, workers)
    matrix = np.vstack(results)
    return sp.csr_matrix(matrix)


def validate_table(table: WorldTable, workers: int = 1, tolerance: float | None = None) -> dict:
    """Fail-closed validation: shapes, signs, both balances, P2 scale, aggregation consistency.

    Row-side balances are computed per country in parallel chunks and merged in sorted order, so
    the reported residuals are identical for any worker count.
    """
    tol = float(table.tolerance if tolerance is None else tolerance)
    checks: dict = {}
    n = table.n
    checks["shape"] = bool(table.Z.shape == (n, n) and table.y.size == n
                           and table.x.size == n and table.va.size == n)
    checks["labels"] = bool(len(set(table.countries)) == len(table.countries)
                            and len(set(table.industries)) == len(table.industries)
                            and table.countries == tuple(sorted(table.countries))
                            and table.industries == tuple(sorted(table.industries)))
    checks["finite"] = bool(np.all(np.isfinite(table.y)) and np.all(np.isfinite(table.x))
                            and np.all(np.isfinite(table.va))
                            and np.all(np.isfinite(table.Z.data)))
    checks["non_negative"] = bool((table.Z.nnz == 0 or float(table.Z.data.min()) >= -tol)
                                  and float(table.y.min()) >= -tol and float(table.va.min()) >= -tol)
    checks["output_non_negative"] = bool(float(table.x.min()) >= -tol)
    p2_scale = bool(table.n_countries >= MIN_COUNTRIES
                    and table.n_industries >= MIN_INDUSTRIES)
    tasks = [(table.Z[table.country_slice(country)], table.y[table.country_slice(country)],
              table.x[table.country_slice(country)]) for country in table.countries]
    chunk_results = _run_chunks(_country_balance_chunk, tasks, workers)
    max_row = max(entry["max_abs_row_residual"] for entry in chunk_results)
    column_residual = np.asarray(table.Z.sum(axis=0)).ravel() + table.va - table.x
    max_column = float(np.max(np.abs(column_residual))) if column_residual.size else 0.0
    checks["product_balance"] = bool(max_row <= tol * max(1.0, float(table.x.max())))
    checks["value_added_balance"] = bool(max_column <= tol * max(1.0, float(table.x.max())))
    Zc = aggregate_country_parallel(table, workers)
    checks["aggregation_totals"] = bool(abs(float(Zc.sum()) - float(table.Z.sum()))
                                        <= tol * max(1.0, abs(float(table.Z.sum()))))
    passed = all(checks.values())
    report = {
        "pass": bool(passed),
        "checks": checks,
        "sectors": n,
        "p2_scale": p2_scale,
        "p2_requirements": {"min_countries": MIN_COUNTRIES, "min_industries": MIN_INDUSTRIES},
        "max_abs_row_residual": max_row,
        "max_abs_value_added_residual": max_column,
        "stored_Z": int(table.Z.nnz),
    }
    if not passed:
        failed = sorted(name for name, value in checks.items() if not value)
        raise PipelineError(f"table failed validation: {failed}")
    return report


# --------------------------------------------------------------------------------------- solver
@dataclass
class WorldSolver:
    """Sparse Leontief solver: one ``splu(I - A)`` reused for the baseline and every scenario."""

    table: WorldTable
    A: sp.csr_matrix = field(init=False)
    spectral_radius: float = field(init=False)
    factor: object = field(init=False, repr=False)
    baseline_x: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.A = self.table.technical_sparse()
        self.spectral_radius = perron_root(self.A)
        if not self.spectral_radius < 1.0 - self.table.tolerance:
            raise PipelineError(
                f"A is not productive: rho(A) = {self.spectral_radius:.12f} >= 1; "
                "(I - A)^-1 does not converge")
        try:
            self.factor = spla.splu(sp.identity(self.table.n, format="csc") - self.A.tocsc())
        except RuntimeError as exc:  # singular matrix
            raise PipelineError(f"(I - A) is singular: {exc}") from exc
        self.baseline_x = self.solve(self.table.y, label="baseline")

    def solve(self, final_demand, label: str = "") -> np.ndarray:
        demand = np.asarray(final_demand, dtype=float).ravel()
        if demand.size != self.table.n:
            raise PipelineError(f"{label or 'solve'}: demand must have {self.table.n} entries")
        if not np.all(np.isfinite(demand)):
            raise PipelineError(f"{label or 'solve'}: demand must be finite")
        solution = np.asarray(self.factor.solve(demand), dtype=float)
        if not np.all(np.isfinite(solution)):
            raise PipelineError(f"{label or 'solve'}: gross output is not finite")
        scale = max(1.0, float(np.max(np.abs(demand))))
        if float(solution.min()) < -self.table.tolerance * scale:
            raise PipelineError(
                f"{label or 'solve'}: infeasible demand, negative gross output "
                f"(min = {float(solution.min()):.6e})")
        return np.maximum(solution, 0.0)

    def material_residual(self, gross_output=None, final_demand=None) -> float:
        output = self.baseline_x if gross_output is None else np.asarray(gross_output, dtype=float)
        demand = self.table.y if final_demand is None else np.asarray(final_demand, dtype=float)
        residual = self.A @ output + demand - output
        return float(np.max(np.abs(residual))) if residual.size else 0.0

    def describe(self) -> dict:
        return {"sectors": self.table.n, "stored_A": int(self.A.nnz),
                "spectral_radius": round(self.spectral_radius, 12),
                "productive": bool(self.spectral_radius < 1.0 - self.table.tolerance),
                "max_abs_material_residual": self.material_residual(),
                "total_output": round(float(self.baseline_x.sum()), 6)}


def perron_root(A: sp.csr_matrix, max_iterations: int = POWER_ITERATIONS,
                tolerance: float = POWER_TOLERANCE) -> float:
    """Perron-Frobenius root of a non-negative sparse matrix by power iteration.

    ``A >= 0`` makes the iteration converge to the Perron root (the largest eigenvalue in modulus),
    which is exactly ``rho(A)``; the dense ``eigvals`` reference is only used on small matrices in
    the tests.  A non-finite or non-positive case raises instead of guessing.
    """
    n = int(A.shape[0])
    if n == 0:
        return 0.0
    if A.nnz and float(A.data.min()) < 0.0:
        raise PipelineError("perron_root requires a non-negative matrix")
    vector = np.full(n, 1.0 / np.sqrt(n))
    previous = 0.0
    for _ in range(int(max_iterations)):
        product = A @ vector
        norm = float(np.linalg.norm(product))
        if norm == 0.0:
            return 0.0
        ratio = norm  # ||A v|| / ||v|| with ||v|| = 1
        vector = product / norm
        if abs(ratio - previous) <= tolerance * max(1.0, ratio):
            return float(ratio)
        previous = ratio
    return float(previous)


def country_core(table: WorldTable) -> dict:
    """Dense reference core on the country block: A = Z/x, rho(A), Hawkins-Simon (io_core)."""
    dataset = table.to_iodataset("country")
    core = LeontiefCore.from_dataset(dataset)
    hawkins = core.hawkins_simon()
    return {
        "level": "country",
        "countries": table.n_countries,
        "aggregated_industries": 1,
        "spectral_radius": round(core.spectral_radius(), 12),
        "productive": bool(core.is_productive()),
        "productiveness_margin": round(core.productiveness_margin(), 12),
        "hawkins_simon_pass": bool(hawkins["pass"]),
        "hawkins_simon_min_pivot": (None if hawkins["min_pivot"] is None
                                    else round(float(hawkins["min_pivot"]), 12)),
        "max_abs_balance_residual": float(dataset.check_balance()),
        "max_members": int(table.n_countries),
    }


# ------------------------------------------------------------------------------------ scenarios
def _normalise_country(country: str | None, table: WorldTable) -> str | None:
    if country is None:
        return None
    if country not in table.countries:
        raise PipelineError(f"unknown country {country!r}; choose one of {list(table.countries)}")
    return country


def resolve_energy_industries(table: WorldTable, explicit=None) -> tuple[str, ...]:
    """Energy industries: explicit list, else keyword match, else the first three industries."""
    if explicit:
        selected = [str(name).strip() for name in explicit if str(name).strip()]
        unknown = sorted(set(selected) - set(table.industries))
        if unknown:
            raise PipelineError(f"unknown energy industries: {unknown}")
        return tuple(selected)
    matched = tuple(name for name in table.industries
                    if any(keyword in name.lower() for keyword in ENERGY_KEYWORDS))
    if matched:
        return matched
    if table.n_industries < 3:
        return table.industries
    return tuple(table.industries[:3])


def _sector_indices(table: WorldTable, industries) -> np.ndarray:
    indices = {name: position for position, name in enumerate(table.industries)}
    selected = [indices[name] for name in industries]
    mask = np.zeros(table.n, dtype=bool)
    for country in range(table.n_countries):
        base = country * table.n_industries
        mask[[base + index for index in selected]] = True
    return np.flatnonzero(mask)


def _delta_aggregates(table: WorldTable, values) -> dict:
    """Aggregate a sector vector to country and industry totals (aggregate-only output)."""
    array = np.asarray(values, dtype=float).ravel()
    by_country = np.asarray(_country_projection(table.n_countries, table.n_industries) @ array).ravel()
    by_industry = np.asarray(_industry_projection(table.n_countries, table.n_industries) @ array).ravel()
    return {"by_country": {name: round(float(value), 6)
                           for name, value in zip(table.countries, by_country)},
            "by_industry": {name: round(float(value), 6)
                            for name, value in zip(table.industries, by_industry)},
            "total": round(float(array.sum()), 6),
            "l1": round(float(np.abs(array).sum()), 6),
            "max_abs": round(float(np.abs(array).max()) if array.size else 0.0, 6)}


def _finish_scenario(table: WorldTable, solver: WorldSolver, name: str, scenario_x,
                     baseline_x, scenario_y, timings: Timings, extra: dict,
                     technology: sp.csr_matrix | None = None) -> dict:
    delta_x = np.asarray(scenario_x, dtype=float) - np.asarray(baseline_x, dtype=float)
    matrix = solver.A if technology is None else technology
    residual = matrix @ np.asarray(scenario_x, dtype=float) + np.asarray(scenario_y, dtype=float) \
        - np.asarray(scenario_x, dtype=float)
    result = {
        "scenario": name,
        "feasible": True,
        "baseline": {"total_output": round(float(np.sum(baseline_x)), 6),
                     "total_final_demand": round(float(np.sum(table.y)), 6)},
        "result": {"total_output": round(float(np.sum(scenario_x)), 6),
                   "total_delta_output": round(float(delta_x.sum()), 6),
                   "max_abs_delta_output": round(float(np.abs(delta_x).max()), 6),
                   "output_multiplier_vs_demand": None},
        "impacts": _delta_aggregates(table, delta_x),
        "balances": {"max_abs_material_residual": round(
            float(np.max(np.abs(residual))) if residual.size else 0.0, 12)},
    }
    if name == "energy" and extra.get("total_delta_final_demand"):
        result["result"]["output_multiplier_vs_demand"] = round(
            float(delta_x.sum()) / float(extra["total_delta_final_demand"]), 6)
    result.update(extra)
    return result


def scenario_energy(table: WorldTable, solver: WorldSolver, *, share: float = 0.05,
                    industries=None, timings: Timings | None = None) -> dict:
    """Energy demand shock: ``delta_y = -share * y`` on the energy sectors, then solve."""
    timings = timings or Timings()
    if not 0.0 < float(share) <= 1.0:
        raise PipelineError("energy share must be in (0, 1]")
    selected = resolve_energy_industries(table, industries)
    with timings.stage("scenario_energy"):
        mask = _sector_indices(table, selected)
        delta_y = np.zeros(table.n)
        delta_y[mask] = -float(share) * table.y[mask]
        scenario_y = table.y + delta_y
        if float(scenario_y.min()) < -table.tolerance:
            raise PipelineError("energy scenario drives final demand negative")
        scenario_x = solver.solve(scenario_y, label="energy")
        extra = {"parameters": {"share": float(share), "industries": list(selected),
                                "sectors_shocked": int(mask.size)},
                 "delta_final_demand": _delta_aggregates(table, delta_y),
                 "total_delta_final_demand": round(float(delta_y.sum()), 6)}
        return _finish_scenario(table, solver, "energy", scenario_x, solver.baseline_x,
                                scenario_y, timings, extra)


def scenario_import(table: WorldTable, solver: WorldSolver, *, share: float = 0.20,
                    timings: Timings | None = None) -> dict:
    """Import-restriction shock: scale the cross-country technology blocks by ``1 - share``.

    ``A' = A + delta_A`` with ``delta_A = -share * A_cross`` (``A_cross`` = entries whose input and
    user are in different countries).  The shocked technology must stay non-negative and productive;
    otherwise the scenario is refused (fail-closed).  The country block is cross-checked with
    ``io_core.technology_shock``.
    """
    timings = timings or Timings()
    if not 0.0 < float(share) < 1.0:
        raise PipelineError("import share must be in (0, 1)")
    with timings.stage("scenario_import"):
        rows = np.repeat(np.arange(table.n), np.diff(table.Z.indptr))
        cols = table.Z.indices
        cross = (rows // table.n_industries) != (cols // table.n_industries)
        mask = sp.csr_matrix((cross.astype(float), (rows, cols)), shape=(table.n, table.n))
        A = solver.A
        delta_A = (A.multiply(mask) * (-float(share))).tocsr()
        A_new = (A + delta_A).tocsr()
        A_new.sum_duplicates()
        A_new.sort_indices()
        if A_new.nnz and float(A_new.data.min()) < -table.tolerance:
            raise PipelineError("import scenario makes a technical coefficient negative")
        rho_new = perron_root(A_new)
        if not rho_new < 1.0 - table.tolerance:
            raise PipelineError(
                f"import scenario breaks productiveness: rho(A') = {rho_new:.12f} >= 1")
        try:
            factor = spla.splu(sp.identity(table.n, format="csc") - A_new.tocsc())
        except RuntimeError as exc:
            raise PipelineError(f"import scenario: (I - A') is singular: {exc}") from exc
        scenario_x = np.asarray(factor.solve(table.y), dtype=float)
        if not np.all(np.isfinite(scenario_x)) or float(scenario_x.min()) < 0.0:
            raise PipelineError("import scenario produced an infeasible solution")
        reference = country_shock_reference(table, share)
        extra = {"parameters": {"share": float(share),
                                "cross_country_stored": int(delta_A.nnz)},
                 "technology": {"delta_A_max_abs": round(
                                    float(np.abs(delta_A.data).max()) if delta_A.nnz else 0.0, 9),
                                "spectral_radius_before": round(solver.spectral_radius, 12),
                                "spectral_radius_after": round(rho_new, 12),
                                "productive_after": True},
                 "country_block_reference": reference}
        return _finish_scenario(table, solver, "import", scenario_x, solver.baseline_x,
                                table.y, timings, extra, technology=A_new)


def country_shock_reference(table: WorldTable, share: float) -> dict:
    """Cross-check the import scenario on the dense country block via ``io_core``."""
    dataset = table.to_iodataset("country")
    block = np.asarray(dataset.Z, dtype=float)
    xc = np.asarray(dataset.x, dtype=float)
    Ablock = block / xc[np.newaxis, :]
    cross = np.ones_like(Ablock) - np.eye(table.n_countries)
    delta_A = -float(share) * Ablock * cross
    try:
        shocked = LeontiefCore(tuple(tuple(row) for row in (Ablock + delta_A)),
                               tuple(dataset.countries), units=dataset.units,
                               tolerance=dataset.tolerance)
        return {"level": "country", "spectral_radius_after": round(shocked.spectral_radius(), 12),
                "productive_after": bool(shocked.is_productive()),
                "hawkins_simon_after": bool(shocked.hawkins_simon()["pass"])}
    except IOCoreError as exc:  # pragma: no cover - defensive
        return {"level": "country", "error": str(exc)}


def scenario_fx(table: WorldTable, solver: WorldSolver, *, country: str | None = None,
                pass_through: float = 0.10, timings: Timings | None = None) -> dict:
    """FX shock: imported input costs in ``country`` rise by ``pass_through`` (currency depreciation).

    ``A'`` scales the cross-country entries in the columns of ``country`` by ``1 + pass_through``.
    The revaluation bookkeeping is checked by ``amfe.world.sfc_core``: one external claim, one
    holder and one issuer, closing = opening + transactions + revaluation, world net worth zero.
    """
    timings = timings or Timings()
    if not -0.99 < float(pass_through) < 1.0:
        raise PipelineError("FX pass-through must be in (-0.99, 1)")
    country = _normalise_country(country, table) or table.countries[0]
    with timings.stage("scenario_fx"):
        country_index = table.countries.index(country)
        own = np.arange(table.n) // table.n_industries
        columns = own == country_index
        rows = np.repeat(np.arange(table.n), np.diff(table.Z.indptr))
        cross = np.asarray(rows // table.n_industries != own[table.Z.indices], dtype=float)
        mask = sp.csr_matrix((cross, (rows, table.Z.indices)), shape=(table.n, table.n))
        A = solver.A
        column_scale = np.where(columns, float(pass_through), 0.0)
        delta_A = ((A.multiply(mask)) @ sp.diags(column_scale)).tocsr()
        A_new = (A + delta_A).tocsr()
        A_new.sum_duplicates()
        A_new.sort_indices()
        if A_new.nnz and float(A_new.data.min()) < -table.tolerance:
            raise PipelineError("FX scenario makes a technical coefficient negative")
        rho_new = perron_root(A_new)
        if not rho_new < 1.0 - table.tolerance:
            raise PipelineError(f"FX scenario breaks productiveness: rho(A') = {rho_new:.12f} >= 1")
        try:
            factor = spla.splu(sp.identity(table.n, format="csc") - A_new.tocsc())
        except RuntimeError as exc:
            raise PipelineError(f"FX scenario: (I - A') is singular: {exc}") from exc
        scenario_x = np.asarray(factor.solve(table.y), dtype=float)
        if not np.all(np.isfinite(scenario_x)) or float(scenario_x.min()) < 0.0:
            raise PipelineError("FX scenario produced an infeasible solution")
        sfc = sfc_fx_check(table, country, float(pass_through))
        extra = {"parameters": {"country": country, "pass_through": float(pass_through)},
                 "technology": {"spectral_radius_before": round(solver.spectral_radius, 12),
                                "spectral_radius_after": round(rho_new, 12),
                                "productive_after": True},
                 "sfc_check": sfc}
        return _finish_scenario(table, solver, "fx", scenario_x, solver.baseline_x,
                                table.y, timings, extra, technology=A_new)


def sfc_fx_check(table: WorldTable, country: str, pass_through: float) -> dict:
    """SFC bookkeeping check of the FX revaluation of the country's external position.

    ``gross_obligation`` = imported intermediates (cross-country inputs) of ``country``; the
    revaluation ``pass_through * gross_obligation`` is booked as one claim with one holder and one
    issuer, so ``SFCFrame`` must accept it (closing identity, zero world net worth, no cross-border
    leak).  This is a consistency check of the entry, not an SFC model of the economy.
    """
    rows = np.repeat(np.arange(table.n), np.diff(table.Z.indptr))
    cols = table.Z.indices
    own = np.arange(table.n) // table.n_industries
    imported = (own[rows] == table.countries.index(country)) & (own[rows] != own[cols])
    gross = float(table.Z.data[imported].sum())
    revaluation = pass_through * gross
    accounts = (f"{country}:EXTERNAL_CLAIM", f"{country}:EXTERNAL_OBLIGATION")
    zeros = [[0.0, 0.0], [0.0, 0.0]]
    opening = [[0.0, gross], [0.0, 0.0]]
    movement = [[0.0, revaluation], [0.0, 0.0]]
    try:
        frame = SFCFrame.from_flows(accounts=accounts, opening=opening,
                                    transactions=zeros, revaluation=movement,
                                    account_countries=(country, country), units=table.units,
                                    tolerance=1e-12)
        consistency = frame.consistency()
        return {"account_countries": [country], "accounts": list(accounts),
                "gross_external_obligation": round(gross, 6),
                "revaluation": round(revaluation, 6),
                "closing_identity_residual": round(
                    float(consistency["closing_identity_residual"]), 12),
                "world_net_worth_residual": round(
                    float(consistency["world_net_worth_residual"]["closing"]), 12),
                "max_abs_residual": round(float(consistency["max_abs_residual"]), 12),
                "pass": bool(consistency["pass"])}
    except SFCError as exc:
        raise PipelineError(f"FX revaluation entry rejected by the SFC core: {exc}") from exc


# -------------------------------------------------------------------------------------- pipeline
def resolve_dataset(args) -> LoadedDataset:
    """Resolve the canonical dataset fail-closed: explicit path/id, env root, then ``data/mrio``."""
    root = getattr(args, "root", None)
    dataset = getattr(args, "dataset", None)
    enforce = not getattr(args, "no_p2_gate", False)
    if dataset:
        candidate = Path(dataset)
        if candidate.is_file():
            return load_dataset(str(candidate), enforce_p2_scale=enforce)
        try:
            return load_dataset(str(dataset), root, enforce_p2_scale=enforce)
        except MRIOError as exc:
            raise BlockedData(str(exc)) from exc
    roots: list[Path] = []
    if root:
        roots.append(Path(root))
    env_root = os.environ.get(DATA_ROOT_ENV, "").strip()
    if env_root:
        roots.append(Path(env_root))
    roots.append(ROOT / "data" / "mrio")
    for candidate_root in roots:
        try:
            ids = available_datasets(candidate_root)
        except MRIOError:
            continue
        for dataset_id in ids:
            try:
                return load_dataset(dataset_id, candidate_root, enforce_p2_scale=enforce)
            except MRIOError:
                continue
    raise BlockedData(
        f"{STATUS_BLOCKED}: no canonical dataset under {[str(path) for path in roots]}; "
        f"set {DATA_ROOT_ENV} or pass --dataset <path|id> (docs/121 §3); "
        "use --synthetic for the built-in benchmark table")


def _scenario_factory(name: str):
    return {"energy": scenario_energy, "import": scenario_import, "fx": scenario_fx}[name]


def run_pipeline(args) -> dict:
    """Execute the requested stages and return the aggregate-only report document."""
    timings = Timings()
    workers = _workers(getattr(args, "workers", None))
    report: dict = {"pipeline": "amfe.world.pipeline", "version": PIPELINE_VERSION,
                    "status": STATUS_OK, "workers": workers,
                    "requested": {"build": bool(getattr(args, "build", False)),
                                  "solve": bool(getattr(args, "solve", False)),
                                  "scenario": getattr(args, "scenario", None)}}
    tracemalloc.start()
    started_at = time.perf_counter()
    try:
        with timings.stage("load"):
            if getattr(args, "synthetic", False):
                table = synthetic_table(int(getattr(args, "countries", 60)),
                                        int(getattr(args, "industries", 45)),
                                        int(getattr(args, "seed", 20261114)),
                                        int(getattr(args, "foreign_entries", 8)),
                                        workers=workers, timings=timings)
                report["dataset"] = {"source": "synthetic"}
            else:
                loaded = resolve_dataset(args)
                table = table_from_canonical(loaded)
                report["dataset"] = {"source": "canonical", "file": str(loaded.path),
                                     "sha256": loaded.sha256, "bytes": loaded.size_bytes}
                report["dataset"]["zero_output_sectors"] = int((table.x <= table.tolerance).sum())
        report["table"] = table.describe()
        with timings.stage("validate"):
            report["validation"] = validate_table(table, workers=workers)
        with timings.stage("core"):
            report["core"] = country_core(table)
        with timings.stage("factorise"):
            solver = WorldSolver(table)
        with timings.stage("solve"):
            report["solve"] = solver.describe()
        report["scenario"] = None
        if getattr(args, "scenario", None):
            function = _scenario_factory(args.scenario)
            share = getattr(args, "share", None)
            if share is None:
                share = 0.20 if args.scenario == "import" else 0.05
            kwargs = {"timings": timings}
            if args.scenario == "energy":
                kwargs.update({"share": float(share),
                               "industries": getattr(args, "energy_industries", None)})
            elif args.scenario == "import":
                kwargs.update({"share": float(share)})
            else:
                kwargs.update({"country": getattr(args, "country", None),
                               "pass_through": float(getattr(args, "pass_through", 0.10))})
            report["scenario"] = function(table, solver, **kwargs)
        report["timings_ms"] = timings.as_dict()
        report["peak_memory_mb"] = peak_memory_mb()
        report["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000.0, 3)
        return report
    except MRIOError as exc:
        raise BlockedData(str(exc)) from exc


def write_report(report: dict, out_dir) -> Path:
    """Write the report deterministically (sorted keys, UTF-8, LF) and return its path."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "world_pipeline_report.json"
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")
    return path


def _print_summary(report: dict) -> None:
    print(f"P2 world pipeline {report['version']} -- status {report['status']}")
    dataset = report.get("dataset") or {}
    print(f"  данные: {dataset.get('source')} {dataset.get('file', '')}".rstrip())
    table = report["table"]
    print(f"  таблица: {table['countries']} стран x {table['industries']} отраслей = "
          f"{table['sectors']} секторов; Z: {table['stored_Z']} ненулевых "
          f"({table['stored_share'] * 100:.2f}%)")
    validation = report["validation"]
    print(f"  валидация: {'PASS' if validation['pass'] else 'FAIL'}; "
          f"невязки {validation['max_abs_row_residual']:.3e} / "
          f"{validation['max_abs_value_added_residual']:.3e}")
    core = report.get("core") or {}
    if core:
        print(f"  страновое ядро: rho(A)={core['spectral_radius']:.9f}, "
              f" Hawkins-Simon={'PASS' if core['hawkins_simon_pass'] else 'FAIL'}")
    solve = report.get("solve") or {}
    if solve:
        print(f"  solve: x=(I-A)^-1 y, total_output={solve['total_output']:,.2f}; "
              f"rho={solve['spectral_radius']:.9f}")
    scenario = report.get("scenario")
    if scenario:
        print(f"  сценарий {scenario['scenario']}: {scenario['result']['total_delta_output']:,.2f} "
              f"изменения выпуска ({scenario['parameters']})")
    print("  тайминги, мс:", ", ".join(f"{name} {value:.1f}"
                                       for name, value in report["timings_ms"].items()))
    print(f"  пик памяти: {report['peak_memory_mb']} МБ; всего {report['elapsed_ms']:.0f} мс")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P2 world pipeline: data -> static core -> scenarios -> report (docs/127)")
    parser.add_argument("--dataset", default=None,
                        help="canonical dataset: path to dataset.json or id under the MRIO root")
    parser.add_argument("--root", default=None, help=f"MRIO data root (default ${DATA_ROOT_ENV})")
    parser.add_argument("--build", action="store_true", help="build/validate the block table")
    parser.add_argument("--solve", action="store_true", help="solve x=(I-A)^-1 y")
    parser.add_argument("--scenario", default=None, choices=("energy", "import", "fx"),
                        help="run one static scenario on top of the baseline")
    parser.add_argument("--share", dest="share", type=float, default=None,
                        help="scenario size: energy demand share (0.05) or import share (0.20)")
    parser.add_argument("--country", default=None, help="FX scenario country (default: first)")
    parser.add_argument("--pass-through", dest="pass_through", type=float, default=0.10,
                        help="FX pass-through to imported input costs (default 0.10)")
    parser.add_argument("--energy-industries", dest="energy_industries", default=None,
                        help="comma-separated energy industry names for the energy scenario")
    parser.add_argument("--synthetic", action="store_true",
                        help="use the deterministic synthetic 60x45 table instead of data")
    parser.add_argument("--countries", type=int, default=60)
    parser.add_argument("--industries", type=int, default=45)
    parser.add_argument("--foreign-entries", dest="foreign_entries", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--workers", type=int, default=None,
                        help="process pool size (default: min(cpu_count, 8))")
    parser.add_argument("--benchmark", action="store_true",
                        help="run build/validate/solve/scenario stage benchmark and print timings")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (build/world)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--no-p2-gate", action="store_true",
                        help="allow a dataset below the P2 scale (tests/fixtures only)")
    return parser


def benchmark(args) -> dict:
    """Time every stage on the synthetic table (default 60x45) and return the report."""
    if not args.synthetic:
        args.synthetic = True
    if args.scenario is None:
        args.scenario = "energy"
    report = run_pipeline(args)
    report["benchmark"] = True
    return report


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.share is None:
        args.share = 0.20 if args.scenario == "import" else 0.05
    if args.energy_industries:
        args.energy_industries = [name.strip() for name in args.energy_industries.split(",")
                                  if name.strip()]
    try:
        report = benchmark(args) if args.benchmark else run_pipeline(args)
    except BlockedData as exc:
        print(f"{STATUS_BLOCKED}: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except (PipelineError, MRIOError) as exc:
        print(f"{STATUS_INVALID}: {exc}", file=sys.stderr)
        return EXIT_INVALID
    path = write_report(report, args.out)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_summary(report)
        print(f"  отчёт: {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
