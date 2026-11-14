"""SFC <-> IO bridge: country income/spending/saving accounts and a period step (P2).

What this module does
---------------------
Turns an MRIO snapshot (canonical ``amfe.mrio.schema.MRIODataset``, the pipeline ``WorldTable``,
or a plain dataset mapping with ``countries/industries/Z|Z_sparse/y/x/va``) into a country-level
stock-flow-consistent accounting block:

    GDP_c        = sum over c's industries of (va + vaa)                 (income side)
    FD_c         = sum over c's product block of (y + fda)               (final demand of c's products)
    EX_int,c     = sum_{j != c} Z[block c, block j]                      (intermediate exports)
    IM_int,c     = sum_{j != c} Z[block j, block c]                      (intermediate imports)
    CA_c         = EX_int,c - IM_int,c  ==  GDP_c - FD_c                 (current account, both sides)
    A_c          = FD_c                                                 (absorption convention)
    S_c          = GDP_c - C_c,   I_c = A_c - C_c,   S_c = I_c + CA_c   (for any split C_c)

The two independent computations of ``CA_c`` must agree exactly (up to the table tolerance): this is
the **identification-free invariant** of the module.  The dataset stores final demand by *producing*
country block (origin convention), so the destination of final goods is not identified; the module
closes the accounts with the documented convention ``A_c = FD_c`` (final demand of c's products is
absorbed in c).  Under that convention ``CA_c`` is the intermediate-trade balance and the world
identity ``sum_c CA_c = 0`` holds exactly.  Real final-goods trade would move absorption across
countries and is **not** in this table (see docs/133, limitations).

Saving/investment split
-----------------------
Only total final demand is stored, so ``C_c`` (consumption) and ``I_c`` (investment) are not
separately identified.  The identity ``S_c = I_c + CA_c`` holds for *any* split (it is a
consequence of ``S - I = GDP - A = CA``); the default is pure absorption-as-consumption
(``investment_share = 0``), and a share can be supplied to obtain non-degenerate S/I magnitudes.

Period step
-----------
``sfc_step`` builds the SFC period step ``closing = opening + transactions + revaluation`` with the
country net foreign asset (NFA) position as the state: transactions are the current-account flows,
financed bilaterally by the documented deficit-pro-rata rule (surplus countries acquire claims on
deficit countries), revaluation is an optional signed vector that must sum to zero (the world is
closed), and the whole step is validated by :class:`amfe.world.sfc_core.SFCFrame` (row/column
totals, world net worth zero, cross-border positions netting out).  No behavioural functions are
implemented: this is bookkeeping only.

Public API
----------
``country_flows``      aggregate an IO dataset to country flows (E, GDP, FD, CA, ...).
``country_accounts``   flows plus consumption/investment/savings with the S=I+CA identity.
``sfc_step``           one SFC period step of the net foreign asset positions.
``sfc_section``        compact JSON-friendly report block for the world pipeline (``--sfc``).
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

from .sfc_core import SFCFrame, SFCError

TOLERANCE = 1e-6
DEFAULT_UNITS = "USD_mn"


class SFCIOError(ValueError):
    """Invalid IO data or a violated SFC/IO accounting identity."""


# ---------------------------------------------------------------------------------- extraction
def _labels(values, name: str) -> tuple[str, ...]:
    labels = tuple(str(value).strip() for value in values)
    if not labels:
        raise SFCIOError(f"{name} must not be empty")
    if any(not label for label in labels):
        raise SFCIOError(f"{name} must not contain empty labels")
    if len(set(labels)) != len(labels):
        raise SFCIOError(f"{name} must be unique")
    return labels


def _vector(values, size: int, name: str, *, signed: bool = False,
            tolerance: float = TOLERANCE) -> np.ndarray:
    try:
        array = np.asarray([float(value) for value in values], dtype=float)
    except (TypeError, ValueError) as exc:
        raise SFCIOError(f"{name} must be a sequence of numbers") from exc
    if array.size != size:
        raise SFCIOError(f"{name} must have {size} entries, got {array.size}")
    if not np.all(np.isfinite(array)):
        raise SFCIOError(f"{name} contains non-finite values")
    if not signed and np.any(array < -tolerance):
        raise SFCIOError(f"{name} must be non-negative")
    return array


def _attribute(dataset, *names):
    for name in names:
        value = getattr(dataset, name, None)
        if value is not None:
            return value
    return None


def _block_exports(z_matrix, countries, industries, *, tolerance: float) -> np.ndarray:
    """Country x country block sums of ``Z`` (dense list/array or a scipy sparse matrix)."""
    n_countries, n_industries = len(countries), len(industries)
    size = n_countries * n_industries
    if hasattr(z_matrix, "tocsr") and hasattr(z_matrix, "shape"):
        matrix = z_matrix.tocsr()
        if matrix.shape != (size, size):
            raise SFCIOError(f"Z must be {size} x {size}, got {matrix.shape}")
        if matrix.data.size and (not np.all(np.isfinite(matrix.data))
                                 or matrix.data.min() < -tolerance):
            raise SFCIOError("Z contains non-finite or negative flows")
        rows = np.arange(size) // n_industries
        cols = np.arange(size)
        import scipy.sparse as sp  # local import: dense inputs do not need scipy

        projection = sp.csr_matrix((np.ones(size), (rows, cols)), shape=(n_countries, size))
        return np.asarray((projection @ matrix @ projection.T).todense(), dtype=float)
    matrix = np.asarray(z_matrix, dtype=float)
    if matrix.shape != (size, size):
        raise SFCIOError(f"Z must be {size} x {size}, got {matrix.shape}")
    if not np.all(np.isfinite(matrix)):
        raise SFCIOError("Z contains non-finite values")
    if np.any(matrix < -tolerance):
        raise SFCIOError("Z contains negative flows")
    blocks = matrix.reshape(n_countries, n_industries, n_countries, n_industries)
    return blocks.sum(axis=(1, 3))


def _sparse_block_exports(z_sparse, countries, industries, *, tolerance: float) -> np.ndarray:
    n_countries, n_industries = len(countries), len(industries)
    size = n_countries * n_industries
    if not isinstance(z_sparse, Mapping):
        raise SFCIOError("Z_sparse must be a mapping with rows/cols/values")
    rows = np.asarray(z_sparse.get("rows", []), dtype=np.int64)
    cols = np.asarray(z_sparse.get("cols", []), dtype=np.int64)
    values = np.asarray(z_sparse.get("values", []), dtype=float)
    if rows.size == 0 or not (rows.size == cols.size == values.size):
        raise SFCIOError("Z_sparse rows/cols/values must have equal non-zero length")
    if rows.min() < 0 or cols.min() < 0 or rows.max() >= size or cols.max() >= size:
        raise SFCIOError("Z_sparse indices are outside the block matrix")
    if not np.all(np.isfinite(values)):
        raise SFCIOError("Z_sparse contains non-finite values")
    if np.any(values < -tolerance):
        raise SFCIOError("Z_sparse contains negative flows")
    cells = (rows // n_industries) * n_countries + (cols // n_industries)
    return np.bincount(cells, weights=values,
                       minlength=n_countries * n_countries).reshape(n_countries, n_countries)


def _extract(dataset) -> dict:
    """Normalise an MRIO dataset (object or mapping) to labels, vectors and the export matrix."""
    if isinstance(dataset, Mapping):
        countries = _labels(dataset.get("countries", ()), "countries")
        industries = _labels(dataset.get("industries", ()), "industries")
        for key in ("y", "x", "va"):
            if key not in dataset:
                raise SFCIOError(f"dataset is missing {key!r}")
        y, x, va = dataset["y"], dataset["x"], dataset["va"]
        fda = dataset.get("final_demand_adjustments", dataset.get("fda", ()))
        vaa = dataset.get("va_adjustments", dataset.get("vaa", ()))
        z = dataset.get("Z")
        z_sparse = dataset.get("Z_sparse")
        if z is None and z_sparse is None:
            raise SFCIOError("dataset must provide Z (dense) or Z_sparse")
        units = str(dataset.get("units", DEFAULT_UNITS))
        tolerance = float(dataset.get("tolerance", TOLERANCE))
    else:
        countries = _labels(_attribute(dataset, "countries") or (), "countries")
        industries = _labels(_attribute(dataset, "industries") or (), "industries")
        y = _attribute(dataset, "y")
        x = _attribute(dataset, "x")
        va = _attribute(dataset, "va")
        z = _attribute(dataset, "Z")
        z_sparse = None
        # WorldTable exposes fda/vaa properties; MRIODataset exposes the signed vectors
        fda = _attribute(dataset, "fda", "final_demand_adjustments")
        vaa = _attribute(dataset, "vaa", "va_adjustments")
        fda = () if fda is None else fda
        vaa = () if vaa is None else vaa
        units = str(getattr(dataset, "units", DEFAULT_UNITS))
        tolerance = float(getattr(dataset, "tolerance", TOLERANCE))
        if y is None or x is None or va is None or z is None:
            raise SFCIOError("dataset object must provide countries, industries, Z, y, x, va")
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise SFCIOError("tolerance must be a positive finite number")
    size = len(countries) * len(industries)
    y_v = _vector(y, size, "y", tolerance=tolerance)
    x_v = _vector(x, size, "x", tolerance=tolerance)
    va_v = _vector(va, size, "va", tolerance=tolerance)
    fda_v = (_vector(fda, size, "final_demand_adjustments", signed=True, tolerance=tolerance)
             if len(tuple(fda)) else np.zeros(size))
    vaa_v = (_vector(vaa, size, "va_adjustments", signed=True, tolerance=tolerance)
             if len(tuple(vaa)) else np.zeros(size))
    if z_sparse is not None and z is None:
        exports = _sparse_block_exports(z_sparse, countries, industries, tolerance=tolerance)
        storage = "sparse_Z"
    else:
        exports = _block_exports(z, countries, industries, tolerance=tolerance)
        storage = "sparse_matrix" if hasattr(z, "tocsr") else "dense_Z"
    return {"countries": countries, "industries": industries, "exports": exports,
            "y": y_v, "x": x_v, "va": va_v, "fda": fda_v, "vaa": vaa_v,
            "units": units, "tolerance": tolerance, "storage": storage}


def _country_sums(vector: np.ndarray, n_countries: int, n_industries: int) -> np.ndarray:
    return vector.reshape(n_countries, n_industries).sum(axis=1)


# --------------------------------------------------------------------------------------- flows
def country_flows(dataset) -> dict:
    """Country-level flows from an IO snapshot, with the two-sided current-account check."""
    data = _extract(dataset)
    countries, industries = data["countries"], data["industries"]
    n_countries, n_industries = len(countries), len(industries)
    exports = data["exports"]
    gdp = _country_sums(data["va"] + data["vaa"], n_countries, n_industries)
    final_demand = _country_sums(data["y"] + data["fda"], n_countries, n_industries)
    intermediate_exports = exports.sum(axis=1)
    intermediate_imports = exports.sum(axis=0)
    current_account_trade = intermediate_exports - intermediate_imports
    current_account_income = gdp - final_demand
    tolerance = data["tolerance"]
    scale = max(1.0, float(np.max(np.abs(gdp))), float(np.max(np.abs(final_demand))))
    residual = current_account_trade - current_account_income
    max_residual = float(np.max(np.abs(residual)))
    if max_residual > tolerance * scale:
        raise SFCIOError(
            "two computations of the current account disagree: max |(EX-IM) - (GDP-FD)| = "
            f"{max_residual:.6e} > tolerance {tolerance * scale:.3e}; the table is inconsistent")
    return {
        "countries": countries, "industries": industries, "units": data["units"],
        "storage": data["storage"], "tolerance": tolerance,
        "gdp": gdp.tolist(), "final_demand": final_demand.tolist(),
        "intermediate_exports": intermediate_exports.tolist(),
        "intermediate_imports": intermediate_imports.tolist(),
        "current_account": current_account_trade.tolist(),
        "current_account_income": current_account_income.tolist(),
        "intermediate_flow_total": float(exports.sum()),
        "checks": {"current_account_disagreement_max_abs": max_residual,
                   "global_current_account_sum": float(current_account_trade.sum()),
                   "gdp_sum_minus_final_demand_sum": float(gdp.sum() - final_demand.sum())},
    }


# ------------------------------------------------------------------------------------ accounts
def country_accounts(dataset, *, investment_share: float = 0.0,
                     consumption: Mapping | None = None) -> dict:
    """Flows plus consumption, investment and savings with the ``S = I + CA`` identity.

    ``investment_share`` sets ``I_c = share * A_c`` and ``C_c = A_c - I_c`` when an explicit
    ``consumption`` mapping is not given; the identity ``S_c - I_c = CA_c`` holds for every split,
    so only the S/I magnitudes depend on the share (documented, not identified by the table).
    """
    share = float(investment_share)
    if not math.isfinite(share) or not 0.0 <= share <= 1.0:
        raise SFCIOError("investment_share must be a finite number in [0, 1]")
    flows = country_flows(dataset)
    countries = flows["countries"]
    gdp = np.asarray(flows["gdp"], dtype=float)
    absorption = np.asarray(flows["final_demand"], dtype=float)
    current_account = np.asarray(flows["current_account"], dtype=float)
    if consumption is not None:
        if set(consumption) - set(countries):
            raise SFCIOError("consumption names unknown countries")
        consumption_values = np.asarray([float(consumption.get(code, 0.0)) for code in countries])
        if np.any(consumption_values < -flows["tolerance"]):
            raise SFCIOError("consumption must be non-negative")
        if np.any(consumption_values > absorption + flows["tolerance"]):
            raise SFCIOError("consumption cannot exceed absorption")
        investment = absorption - consumption_values
    else:
        investment = share * absorption
        consumption_values = absorption - investment
    savings = gdp - consumption_values
    identity = savings - investment - current_account
    scale = max(1.0, float(np.max(np.abs(gdp))))
    max_identity = float(np.max(np.abs(identity)))
    if max_identity > flows["tolerance"] * scale:
        raise SFCIOError(
            f"S = I + CA violated: max |S - I - CA| = {max_identity:.6e}")
    return {
        **flows,
        "absorption": absorption.tolist(),
        "consumption": consumption_values.tolist(),
        "investment": investment.tolist(),
        "savings": savings.tolist(),
        "investment_share": share,
        "checks": {**flows["checks"],
                   "saving_investment_identity_max_abs": max_identity,
                   "current_account_sum": float(current_account.sum()),
                   "world_savings_minus_investment": float(savings.sum() - investment.sum())},
    }


def accounts_table(accounts: dict) -> dict:
    """Per-country rows of the accounts block (report convenience view)."""
    countries = accounts["countries"]
    keys = ("gdp", "final_demand", "intermediate_exports", "intermediate_imports",
            "current_account", "consumption", "investment", "savings")
    return {code: {key: round(float(accounts[key][index]), 6) for key in keys}
            for index, code in enumerate(countries)}


# ---------------------------------------------------------------------------------- period step
def _bilateral_matrix(countries, positions, *, tolerance: float) -> np.ndarray:
    """Bilateral claim matrix reproducing net positions (deficits financed pro rata by surpluses)."""
    values = np.asarray(positions, dtype=float)
    if values.size != len(countries):
        raise SFCIOError("positions must have one value per country")
    if not np.all(np.isfinite(values)):
        raise SFCIOError("positions contain non-finite values")
    total = float(values.sum())
    scale = max(1.0, float(np.max(np.abs(values))))
    if abs(total) > tolerance * scale:
        raise SFCIOError(f"net positions must sum to zero, got {total:.6e}")
    surplus = [(index, float(value)) for index, value in enumerate(values) if value > tolerance]
    deficit = [(index, -float(value)) for index, value in enumerate(values) if value < -tolerance]
    matrix = np.zeros((len(countries), len(countries)), dtype=float)
    total_deficit = math.fsum(value for _, value in deficit)
    if total_deficit <= 0.0:
        return matrix
    for holder, surplus_value in surplus:
        for issuer, deficit_value in deficit:
            matrix[holder][issuer] = surplus_value * deficit_value / total_deficit
    return matrix


def sfc_step(accounts: dict, *, opening=None, revaluation=None,
             tolerance: float | None = None) -> dict:
    """One SFC period step of country net foreign assets: closing = opening + CA + revaluation."""
    countries = accounts["countries"]
    tol = float(accounts["tolerance"] if tolerance is None else tolerance)
    current_account = np.asarray(accounts["current_account"], dtype=float)
    if opening is None:
        opening_values = np.zeros(len(countries))
    elif isinstance(opening, Mapping):
        missing = set(countries) - set(opening)
        if missing:
            raise SFCIOError(f"opening is missing countries: {sorted(missing)}")
        opening_values = np.asarray([float(opening[code]) for code in countries])
    else:
        opening_values = np.asarray(opening, dtype=float)
        if opening_values.size != len(countries):
            raise SFCIOError("opening must have one value per country")
    if revaluation is None:
        revaluation_values = np.zeros(len(countries))
    elif isinstance(revaluation, Mapping):
        missing = set(countries) - set(revaluation)
        if missing:
            raise SFCIOError(f"revaluation is missing countries: {sorted(missing)}")
        revaluation_values = np.asarray([float(revaluation[code]) for code in countries])
    else:
        revaluation_values = np.asarray(revaluation, dtype=float)
        if revaluation_values.size != len(countries):
            raise SFCIOError("revaluation must have one value per country")
    scale = max(1.0, float(np.max(np.abs(opening_values))), float(np.max(np.abs(current_account))))
    for name, values in (("opening", opening_values), ("revaluation", revaluation_values)):
        if not np.all(np.isfinite(values)):
            raise SFCIOError(f"{name} contains non-finite values")
        if abs(float(values.sum())) > tol * scale:
            raise SFCIOError(
                f"{name} must sum to zero in a closed world, got {float(values.sum()):.6e}")
    opening_matrix = _bilateral_matrix(countries, opening_values, tolerance=tol)
    transaction_matrix = _bilateral_matrix(countries, current_account, tolerance=tol)
    revaluation_matrix = _bilateral_matrix(countries, revaluation_values, tolerance=tol)
    try:
        frame = SFCFrame.from_flows(
            accounts=countries, account_countries=countries, opening=opening_matrix.tolist(),
            transactions=transaction_matrix.tolist(), revaluation=revaluation_matrix.tolist(),
            units=accounts["units"], tolerance=max(tol, 1e-9))
    except SFCError as exc:
        raise SFCIOError(f"SFC frame refused the period step: {exc}") from exc
    closing = frame.net_worths("closing")
    return {
        "countries": countries, "units": accounts["units"],
        "opening": {code: round(float(value), 6)
                    for code, value in zip(countries, opening_values)},
        "transactions": {code: round(float(value), 6)
                         for code, value in zip(countries, current_account)},
        "revaluation": {code: round(float(value), 6)
                        for code, value in zip(countries, revaluation_values)},
        "closing": {code: round(float(value), 6) for code, value in closing.items()},
        "totals": {"opening": float(opening_values.sum()),
                   "transactions": float(current_account.sum()),
                   "revaluation": float(revaluation_values.sum()),
                   "closing": float(math.fsum(closing.values()))},
        "identity_residual": frame.closing_identity_residual(),
        "net_worth_reconciliation_residual": frame.net_worth_residual(),
        "sfc_consistency": frame.consistency(),
        "pass": bool(frame.consistency()["pass"] and abs(float(math.fsum(closing.values()))) <= tol * scale),
    }


# -------------------------------------------------------------------------------------- report
def sfc_section(dataset, *, investment_share: float = 0.0, opening=None,
                revaluation=None, include_countries: bool = False,
                tolerance: float | None = None) -> dict:
    """Compact JSON-friendly SFC block for the world pipeline (``--sfc``)."""
    accounts = country_accounts(dataset, investment_share=investment_share, consumption=None)
    step = sfc_step(accounts, opening=opening, revaluation=revaluation, tolerance=tolerance)
    countries = accounts["countries"]
    current_account = accounts["current_account"]
    ordered = sorted(range(len(countries)), key=lambda index: (-current_account[index],
                                                             countries[index]))
    top_surplus = [[countries[index], round(float(current_account[index]), 6)]
                   for index in ordered[:5] if current_account[index] > 0.0]
    top_deficit = [[countries[index], round(float(current_account[index]), 6)]
                   for index in reversed(ordered[-5:]) if current_account[index] < 0.0]
    checks = dict(accounts["checks"])
    checks.update({
        "net_foreign_assets_opening_sum": step["totals"]["opening"],
        "net_foreign_assets_closing_sum": step["totals"]["closing"],
        "sfc_identity_residual": step["identity_residual"],
        "sfc_net_worth_residual": step["net_worth_reconciliation_residual"],
        "sfc_pass": step["pass"],
    })
    section = {
        "module": "amfe.world.sfc_io",
        "method": "IO country accounts + SFC net foreign asset period step",
        "units": accounts["units"],
        "countries": len(countries),
        "storage": accounts.get("storage"),
        "investment_share": accounts["investment_share"],
        "totals": {
            "gdp": round(float(sum(accounts["gdp"])), 6),
            "final_demand": round(float(sum(accounts["final_demand"])), 6),
            "savings": round(float(sum(accounts["savings"])), 6),
            "investment": round(float(sum(accounts["investment"])), 6),
            "current_account": round(float(sum(current_account)), 6),
            "net_foreign_assets": round(step["totals"]["closing"], 6),
        },
        "current_account": {"top_surplus": top_surplus, "top_deficit": top_deficit},
        "checks": checks,
    }
    section["assumptions"] = [
        "final demand is stored by producing country block; absorption convention A_c = FD_c",
        "final-goods destination is not identified by the dataset; CA is the intermediate-trade "
        "balance",
        "consumption/investment are not identified; S = I + CA holds for any split, the share "
        "only sets the magnitudes",
        "opening net foreign assets are zero unless supplied; no behavioural equations",
        "the SFC frame treats the NFA position as the only asset; domestic assets net to zero",
    ]
    scale = max(1.0, abs(float(sum(accounts["gdp"]))), abs(float(sum(accounts["final_demand"]))))
    tolerance_scale = accounts["tolerance"] * scale
    section_pass = bool(
        abs(checks["global_current_account_sum"]) <= tolerance_scale
        and abs(checks["gdp_sum_minus_final_demand_sum"]) <= tolerance_scale
        and checks["saving_investment_identity_max_abs"] <= tolerance_scale
        and abs(checks["current_account_disagreement_max_abs"]) <= tolerance_scale
        and abs(checks["net_foreign_assets_opening_sum"]) <= tolerance_scale
        and abs(checks["net_foreign_assets_closing_sum"]) <= tolerance_scale
        and step["pass"])
    section["pass"] = section_pass
    if include_countries:
        section["accounts"] = accounts_table(accounts)
    return section


__all__ = ["SFCIOError", "accounts_table", "country_accounts", "country_flows", "sfc_section",
           "sfc_step"]
