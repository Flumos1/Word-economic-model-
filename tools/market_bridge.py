#!/usr/bin/env python3
"""Bridge: market price shock (oil / Hormuz) -> MRIO Leontief prices (docs/152, G2).

What this tool does — and does not do
-------------------------------------
It translates the deterministic Brent scenario of :class:`amfe.markets.oil.OilModule`
(``hormuz_closed_6m``) into a **relative producer-price change** by country-industry through the
Leontief price model

    Δp = (I − Aᵀ)⁻¹ c ,      c_i = pass_through · Δbrent% · Σ_b share_b · s_{i,b}(A)

where ``A = Z / x`` is the fixed intermediate-input matrix of a canonical MRIO dataset,
``share_b`` are the oil-cost shares of the generic cost buckets returned by
``OilModule.prepare_cost_shock`` (energy / transport / manufacturing / services) and ``s_{i,b}``
is the share of bucket ``b`` in the total intermediate cost of industry ``i`` computed from ``A``.
It is a **price pass-through** calculation under constant returns, fixed technology and full
pass-through of unit costs: no quantities, no substitution, no demand response, no dynamics.

The quantity-side effect is reported **separately** as a *quantity proxy* taken from the existing
AMFE comparative-static scenario (``energy_plus50`` / ``energy_minus5`` in the scenario report
indexes).  The two objects are never combined: the bridge does not produce an output forecast.

All results are ``RESEARCH_ONLY``; nothing here is an admission, a forecast or investment advice.

Usage
-----
    python tools/market_bridge.py                      # FIGARO 2019 + OECD 2025 (if present)
    python tools/market_bridge.py --dataset figaro
    python tools/market_bridge.py --pass-through 0.3,0.6,1.0 --out build/market_bridge
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

SCHEMA = "amfe.markets.mrio_bridge.v1"
STATUS = "RESEARCH_ONLY"
DEFAULT_OUT = ROOT / "build" / "market_bridge"
FALLBACK_DATA_ROOT = Path("F:/Мировая экономика модель/data/mrio")
FIGARO_DATASET = "figaro/figaro_2019/dataset.json"
OECD_2025_DATASET = "oecd_icio_2025/datasets/2019.json"
DEFAULT_PASS_THROUGH = (0.3, 0.6, 1.0)
DEFAULT_SCENARIO = "hormuz_closed_6m"
#: Oil-cost shares by generic bucket (the ``sector_shares`` of the market bridge; docs/147).
DEFAULT_BUCKET_SHARES = {"energy": 0.30, "transport": 0.12, "manufacturing": 0.04,
                         "services": 0.02}
#: Industry -> cost bucket rules for the two MRIO classifications.
BUCKET_RULES = {
    "figaro": {
        "energy": ("CPA_B", "CPA_C19", "CPA_D35"),
        "transport": ("CPA_H49", "CPA_H50", "CPA_H51", "CPA_H52", "CPA_H53"),
        "manufacturing_prefixes": ("CPA_C",),
        "manufacturing_exclude": ("CPA_C19",),
    },
    "oecd_2025": {
        "energy": ("B05", "B06", "B07", "B08", "B09", "C19", "D"),
        "transport": ("H49", "H50", "H51", "H52", "H53"),
        "manufacturing_prefixes": ("C",),
        "manufacturing_exclude": ("C19",),
    },
}


class BridgeError(RuntimeError):
    """A named failure: the bridge must not produce a number."""


# --------------------------------------------------------------------------------- provenance
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_data_root(explicit=None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            raise BridgeError(f"data root is not a directory: {path}")
        return path
    env = str(__import__("os").environ.get("AMFE_MRIO_ROOT", "")).strip()
    if env and Path(env).is_dir():
        return Path(env)
    if FALLBACK_DATA_ROOT.is_dir():
        return FALLBACK_DATA_ROOT
    return ROOT / "data" / "mrio"


# ----------------------------------------------------------------------------------- dataset
def load_dataset_matrix(path: Path) -> dict:
    """Load a canonical dataset document (dense ``Z`` or sparse ``Z_sparse``) and its matrix.

    Returns a dict with ``countries``, ``industries``, ``x``, ``A`` (scipy sparse), ``n``,
    ``meta``.  Fail-closed on non-positive output, missing fields or a non-finite matrix.
    """
    import scipy.sparse as sp

    path = Path(path)
    if not path.is_file():
        raise BridgeError(f"dataset not found: {path}")
    started = time.monotonic()
    document = json.loads(path.read_text(encoding="utf-8"))
    countries = [str(name) for name in document.get("countries") or []]
    industries = [str(name) for name in document.get("industries") or []]
    sizes = document.get("sizes") or {}
    if not countries or not industries:
        # sparse packages may carry sizes only
        if not document.get("sizes"):
            raise BridgeError(f"{path}: countries/industries are missing")
        countries = [str(name) for name in sizes.get("countries") or countries]
        industries = [str(name) for name in sizes.get("industries") or industries]
    x = np.asarray(document.get("x") or [], dtype=float)
    n = len(countries) * len(industries)
    if len(x) != n or n == 0:
        raise BridgeError(f"{path}: x has {len(x)} entries for {n} sectors")
    if not np.all(np.isfinite(x)):
        raise BridgeError(f"{path}: x contains non-finite values")
    # Some canonical packages keep sectors with exactly zero output (not counted blocks such as
    # the FIGARO ``DOM`` aggregate).  They cannot enter a price model; they are dropped only if
    # their Z row and column are empty, otherwise the document is refused (fail-closed).
    zero_or_negative = np.where(x <= 0.0)[0]
    if "Z_sparse" in document:
        sparse = document["Z_sparse"]
        rows = np.asarray(sparse.get("rows") or [], dtype=np.int64)
        cols = np.asarray(sparse.get("cols") or [], dtype=np.int64)
        values = np.asarray(sparse.get("values") or [], dtype=float)
        if not (len(rows) == len(cols) == len(values)):
            raise BridgeError(f"{path}: malformed Z_sparse arrays")
        Z = sp.coo_matrix((values, (rows, cols)), shape=(n, n)).tocsr()
        storage = "sparse"
    elif "Z" in document:
        Z = sp.csr_matrix(np.asarray(document["Z"], dtype=float))
        storage = "dense"
    else:
        raise BridgeError(f"{path}: neither Z nor Z_sparse present")
    if Z.shape != (n, n):
        raise BridgeError(f"{path}: Z shape {Z.shape} != ({n}, {n})")
    if Z.nnz and not np.all(np.isfinite(Z.data)):
        raise BridgeError(f"{path}: Z contains non-finite values")
    dropped = []
    if len(zero_or_negative):
        for index in zero_or_negative:
            row_nnz = int(Z[index, :].nnz)
            column_nnz = int(Z[:, index].nnz)
            if row_nnz or column_nnz:
                raise BridgeError(
                    f"{path}: sector {index} has zero output but non-empty Z "
                    f"(row nnz {row_nnz}, column nnz {column_nnz}); refusing the price model")
            dropped.append({"index": int(index),
                            "country": countries[index // len(industries)],
                            "industry": industries[index % len(industries)]})
        kept = np.setdiff1d(np.arange(n), zero_or_negative)
        labels = [{"country": countries[index // len(industries)],
                   "industry": industries[index % len(industries)]} for index in kept]
        x = x[kept]
        Z = Z[kept][:, kept]
        n_active = len(kept)
    else:
        kept = np.arange(n)
        labels = [{"country": countries[index // len(industries)],
                   "industry": industries[index % len(industries)]} for index in range(n)]
        n_active = n
    # B[i, j] = Z[i, j] / x[j]; store M = B^T (A[i, j] = Z[j, i] / x[i]) so that the price model
    # is (I - A) p = c and the upstream propagation p_{k+1} = c + A p_k.
    A = (sp.diags(1.0 / x) @ Z.T).tocsc()
    return {"path": str(path), "countries": countries, "industries": industries,
            "n": n_active, "n_full": n, "kept": kept, "sector_labels": labels,
            "dropped_sectors": dropped, "x": x, "Z": Z, "A": A, "storage": storage,
            "schema": document.get("schema"), "units": document.get("units"),
            "years": list(document.get("years") or []), "bytes": path.stat().st_size,
            "sha256": sha256_file(path), "load_seconds": round(time.monotonic() - started, 1)}


def bucket_weights(classification: str, industries: list[str]) -> dict:
    """Map each industry to cost-bucket membership weights (1.0 on its bucket, else 0)."""
    rules = BUCKET_RULES.get(classification)
    if rules is None:
        raise BridgeError(f"unknown industry classification {classification!r}; "
                          f"known: {sorted(BUCKET_RULES)}")
    weights: dict[str, dict] = {}
    for industry in industries:
        weights[industry] = {"energy": 0.0, "transport": 0.0, "manufacturing": 0.0,
                             "services": 0.0}
    energy = set(rules["energy"])
    transport = set(rules["transport"])
    prefixes = tuple(rules["manufacturing_prefixes"])
    excluded = set(rules["manufacturing_exclude"])
    for industry in industries:
        if industry in energy:
            weights[industry]["energy"] = 1.0
        elif industry in transport:
            weights[industry]["transport"] = 1.0
        elif industry.startswith(prefixes) and industry not in excluded:
            weights[industry]["manufacturing"] = 1.0
        else:
            weights[industry]["services"] = 1.0
    uncovered = [industry for industry, row in weights.items() if sum(row.values()) != 1.0]
    if uncovered:  # defensive: rules are exhaustive by construction
        raise BridgeError(f"industries without a cost bucket: {uncovered[:5]}")
    return weights


def bucket_cost_shares(matrix: dict, weights: dict) -> dict:
    """Share of each bucket in the total intermediate cost of every sector: s_{i,b}(A)."""
    A = matrix["A"]
    sector_rows = {bucket: [] for bucket in ("energy", "transport", "manufacturing", "services")}
    for position, label in enumerate(matrix["sector_labels"]):
        for bucket, weight in weights[label["industry"]].items():
            if weight:
                sector_rows[bucket].append(position)
    shares = {}
    for bucket, positions in sector_rows.items():
        if not positions:
            shares[bucket] = np.zeros(matrix["n"])
            continue
        # s_{i,b} = sum_{j in bucket} A[j, i]  (A is stored as B^T: A[i, j] = Z[j, i] / x[i]),
        # i.e. the bucket's share in the total intermediate cost of the using sector i.
        bucket_inputs = np.asarray(A[:, positions].sum(axis=1)).ravel()
        shares[bucket] = bucket_inputs
    return shares


# -------------------------------------------------------------------------------- oil -> costs
def run_oil_scenario(scenario_id: str = DEFAULT_SCENARIO) -> dict:
    """Run the oil module and build the MRIO-ready cost shock (fail-closed on module errors)."""
    from amfe.markets import oil as oil_module
    from amfe.markets.contract import MarketError

    module = oil_module.OilModule()
    try:
        result = module.run(scenario_id)
        shock = module.prepare_cost_shock(result, pass_through=1.0)
    except MarketError as error:
        raise BridgeError(f"oil module refused the scenario: {error}") from error
    summary = result.summary
    peak_range = summary.get("brent_range_usd_per_bbl") or {}
    mean_range = summary.get("brent_range_mean_usd_per_bbl") or {}
    return {
        "module_id": result.module_id, "scenario_id": result.scenario_id,
        "units": result.units, "horizon_months": result.horizon_months,
        "status": result.status,
        "price_path": [round(float(value), 6) for value in result.price_path],
        "base_price": float(result.meta["parameters"]["base_price"]),
        "price_peak": float(summary["price_peak"]),
        "price_trough": float(summary.get("price_trough") or min(result.price_path)),
        "price_mean": float(summary["price_mean"]),
        "price_low": float(peak_range.get("low") or summary["price_peak"]),
        "price_high": float(peak_range.get("high") or summary["price_peak"]),
        "mean_range_usd_bbl": {"low": mean_range.get("low"), "central": mean_range.get("central"),
                               "high": mean_range.get("high")},
        "brent_delta_usd": float(shock.brent_delta_usd),
        "brent_delta_pct": float(shock.brent_delta_pct),
        "sector_cost_multipliers_unit_passthrough": dict(shock.sector_cost_multipliers),
        "assumptions": [item.as_dict() for item in result.assumptions],
        "sensitivity": list(result.sensitivity),
        "checks": dict(result.checks),
        "digest": _digest(result.as_dict() if hasattr(result, "as_dict") else summary),
    }


def cost_vector(matrix: dict, *, pass_through: float, brent_delta_pct: float,
                bucket_shares: dict, weights: dict) -> tuple:
    """Unit-cost shock vector ``c`` and the bucket cost-share vectors used to build it."""
    shares = bucket_cost_shares(matrix, weights)
    if not 0.0 <= float(pass_through) <= 1.0:
        raise BridgeError(f"pass_through must be in [0, 1], got {pass_through}")
    for bucket, share in bucket_shares.items():
        if bucket not in shares:
            raise BridgeError(f"unknown cost bucket {bucket!r}")
        if not 0.0 <= float(share) <= 1.0:
            raise BridgeError(f"bucket share for {bucket} must be in [0, 1]")
    oil_cost_share = np.zeros(matrix["n"])
    for bucket in ("energy", "transport", "manufacturing", "services"):
        oil_cost_share += float(bucket_shares.get(bucket, 0.0)) * shares[bucket]
    c = float(pass_through) * float(brent_delta_pct) * oil_cost_share
    if not np.all(np.isfinite(c)):
        raise BridgeError("cost vector contains non-finite values")
    if np.any(c < 0.0):
        raise BridgeError("cost vector contains negative entries")
    return c, shares


# ---------------------------------------------------------------------------- price model
def spectral_radius(matrix: dict, tolerance: float = 1e-10,
                    max_iterations: int = 5000) -> dict:
    """Perron root of the non-negative matrix A: ARPACK first, power iteration as fallback."""
    A = matrix["A"]
    n = matrix["n"]
    try:
        from scipy.sparse.linalg import eigs
        values = eigs(A.astype(float), k=1, which="LM", maxiter=2000,
                      return_eigenvectors=False, tol=1e-12)
        if values.size:
            eigenvalue = float(np.max(np.abs(values)))
            return {"spectral_radius": round(eigenvalue, 12), "iterations": None,
                    "converged": True, "method": "scipy.sparse.linalg.eigs", "note": None}
    except Exception:  # pragma: no cover - ARPACK fallback
        pass
    vector = np.full(n, 1.0 / n)
    eigenvalue = 0.0
    for iteration in range(max_iterations):
        product = A @ vector
        norm = float(np.linalg.norm(product, 1))
        if norm == 0.0:
            return {"spectral_radius": 0.0, "iterations": iteration + 1, "converged": True,
                    "method": "power", "note": "A is nilpotent"}
        vector = product / norm
        new_eigenvalue = float(np.linalg.norm(A @ vector, 1))
        if abs(new_eigenvalue - eigenvalue) <= tolerance * max(1.0, abs(new_eigenvalue)):
            return {"spectral_radius": round(new_eigenvalue, 12),
                    "iterations": iteration + 1, "converged": True, "method": "power",
                    "note": None}
        eigenvalue = new_eigenvalue
    return {"spectral_radius": round(eigenvalue, 12), "iterations": max_iterations,
            "converged": False, "method": "power",
            "note": "power iteration did not converge"}


def solve_prices(matrix: dict, c: np.ndarray, *, tolerance: float = 1e-10) -> dict:
    """Solve ``(I − Aᵀ) p = c`` directly and by Neumann iteration; cross-check both."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import spsolve

    A = matrix["A"]
    n = matrix["n"]
    identity = sp.identity(n, format="csc")
    system = (identity - A).tocsc()  # A is stored transposed already (A[i, j] = Z[i, j]/x[j])
    try:
        prices = spsolve(system, c)
    except Exception as error:  # pragma: no cover - numerical failure
        raise BridgeError(f"price solve failed: {error}") from error
    prices = np.asarray(prices, dtype=float).ravel()
    if not np.all(np.isfinite(prices)):
        raise BridgeError("price solution contains non-finite values")
    if np.any(prices < -tolerance):
        raise BridgeError(f"price solution has negative entries (min {float(prices.min()):.6g})")
    # Neumann cross-check: p_{k+1} = c + Aᵀ p_k (A already transposed -> A @ p_k)
    iterative = np.array(c, dtype=float)
    residual = None
    for iteration in range(2000):
        updated = c + A @ iterative
        residual = float(np.max(np.abs(updated - iterative)))
        iterative = updated
        if residual <= tolerance:
            break
    difference = float(np.max(np.abs(iterative - prices)))
    return {"prices": prices, "neumann_iterations": iteration + 1,
            "neumann_residual": residual, "direct_vs_neumann_max_abs": difference,
            "converged": residual is not None and residual <= max(tolerance * 100.0, 1e-8)}


def price_summary(matrix: dict, prices: np.ndarray, top_n: int = 10) -> dict:
    """Aggregate Δprice by industry, country and pair (output-weighted means)."""
    x = matrix["x"]
    labels = matrix["sector_labels"]
    sector_country = [item["country"] for item in labels]
    sector_industry = [item["industry"] for item in labels]
    total_x = float(x.sum())
    by_industry: dict[str, float] = {}
    by_country: dict[str, float] = {}
    weight_by_industry: dict[str, float] = {}
    weight_by_country: dict[str, float] = {}
    for position, price in enumerate(prices):
        value = float(x[position])
        industry, country = sector_industry[position], sector_country[position]
        by_industry[industry] = by_industry.get(industry, 0.0) + price * value
        weight_by_industry[industry] = weight_by_industry.get(industry, 0.0) + value
        by_country[country] = by_country.get(country, 0.0) + price * value
        weight_by_country[country] = weight_by_country.get(country, 0.0) + value
    industry_means = {name: by_industry[name] / weight_by_industry[name]
                      for name in by_industry if weight_by_industry[name]}
    country_means = {name: by_country[name] / weight_by_country[name]
                     for name in by_country if weight_by_country[name]}

    def ranked(mapping: dict) -> list[dict]:
        ordered = sorted(mapping.items(), key=lambda item: (-item[1], item[0]))[:top_n]
        return [{"name": name, "delta_price_pct": round(value * 100.0, 6),
                 "delta_price_relative": round(value, 9)} for name, value in ordered]

    pairs = []
    for position, price in enumerate(prices):
        pairs.append({"country": sector_country[position], "industry": sector_industry[position],
                      "delta_price_pct": round(float(price) * 100.0, 6),
                      "delta_price_relative": round(float(price), 9),
                      "output_weight_usd_mn": round(float(x[position]), 3)})
    pairs.sort(key=lambda item: (-item["delta_price_pct"], item["country"], item["industry"]))
    aggregate_index = float(np.dot(prices, x) / total_x) if total_x else 0.0
    return {
        "sectors": matrix["n"],
        "aggregate_price_index_pct": round(aggregate_index * 100.0, 6),
        "max_delta_price_pct": round(float(prices.max()) * 100.0, 6),
        "min_delta_price_pct": round(float(prices.min()) * 100.0, 6),
        "median_delta_price_pct": round(float(np.median(prices)) * 100.0, 6),
        "top_industries": ranked(industry_means),
        "top_countries": ranked(country_means),
        "top_country_industry": pairs[:top_n],
        "n_industries": len(industry_means),
        "n_countries": len(country_means),
    }


def run_price_model(matrix: dict, *, classification: str, oil: dict,
                    pass_through: float, bucket_shares: dict | None = None) -> dict:
    """Full Leontief price pass-through for one dataset and one pass-through level."""
    weights = bucket_weights(classification, matrix["industries"])
    shares = bucket_shares or DEFAULT_BUCKET_SHARES
    started = time.monotonic()
    c, bucket_shares_used = cost_vector(matrix, pass_through=pass_through,
                                        brent_delta_pct=oil["brent_delta_pct"],
                                        bucket_shares=shares, weights=weights)
    radius = spectral_radius(matrix)
    if not radius["converged"]:
        raise BridgeError(f"spectral radius did not converge: {radius}")
    if radius["spectral_radius"] >= 1.0:
        raise BridgeError(f"A is not productive (spectral radius {radius['spectral_radius']}); "
                          f"the Leontief price model would not converge")
    solution = solve_prices(matrix, c)
    if not solution["converged"]:
        raise BridgeError("Neumann price iteration did not converge; refusing a price vector")
    if solution["direct_vs_neumann_max_abs"] > 1e-6:
        raise BridgeError(
            f"direct price solution and Neumann propagation disagree by "
            f"{solution['direct_vs_neumann_max_abs']:.3g}; refusing the price vector")
    prices = solution["prices"]
    if np.max(prices) > 5.0:
        raise BridgeError(f"implausible price change (max {100 * float(np.max(prices)):.0f}%); "
                          f"refusing to report it")
    summary = price_summary(matrix, prices)
    checks = {
        "productive": radius["spectral_radius"] < 1.0,
        "spectral_radius": radius["spectral_radius"],
        "spectral_radius_iterations": radius["iterations"],
        "spectral_radius_method": radius.get("method"),
        "prices_finite": bool(np.all(np.isfinite(prices))),
        "prices_non_negative": bool(np.all(prices >= -1e-12)),
        "neumann_converged": bool(solution["converged"]),
        "direct_vs_neumann_max_abs": solution["direct_vs_neumann_max_abs"],
        "pass_through": float(pass_through),
        "linear_in_pass_through": True,
        "plausible_bound_max_pct": 500.0,
        "bridge_pass": bool(radius["spectral_radius"] < 1.0 and solution["converged"]
                            and np.all(np.isfinite(prices))),
    }
    return {
        "dataset": {"path": matrix["path"], "sha256": matrix["sha256"],
                    "bytes": matrix["bytes"], "countries": len(matrix["countries"]),
                    "industries": len(matrix["industries"]), "sectors": matrix["n"],
                    "sectors_full": matrix["n_full"],
                    "dropped_zero_output_sectors": len(matrix["dropped_sectors"]),
                    "years": matrix["years"], "units": matrix["units"],
                    "storage": matrix["storage"], "classification": classification,
                    "load_seconds": matrix["load_seconds"]},
        "pass_through": float(pass_through),
        "bucket_shares": dict(shares),
        "bucket_cost_share_mean": {bucket: round(float(value.mean()), 9)
                                   for bucket, value in bucket_shares_used.items()},
        "cost_vector": {"min": round(float(c.min()), 9), "max": round(float(c.max()), 9),
                        "mean": round(float(c.mean()), 9)},
        "checks": checks,
        "results": summary,
        "seconds": round(time.monotonic() - started, 1),
    }


# ------------------------------------------------------------------------------ quantity proxy
def quantity_proxy(index_path: Path, label: str) -> dict:
    """Read the existing AMFE comparative-static energy scenarios as a separate proxy only."""
    path = Path(index_path)
    if not path.is_file():
        return {"label": label, "status": "NOT_AVAILABLE", "path": str(path),
                "note": "scenario report index not found; quantity proxy omitted (price model "
                        "does not produce output numbers)"}
    document = json.loads(path.read_text(encoding="utf-8"))
    scenarios = {str(item.get("id")): item for item in document.get("scenarios") or []}
    output = {}
    for scenario_id in ("energy_minus5", "energy_plus50"):
        entry = scenarios.get(scenario_id)
        if not entry:
            continue
        headline = entry.get("headline") or {}
        output[scenario_id] = {
            "total_delta_output_usd_mn": headline.get("total_delta_output"),
            "relative_delta_output": headline.get("relative_delta_output"),
            "largest_country": headline.get("largest_country"),
            "largest_industry": headline.get("largest_industry"),
        }
    dataset = document.get("dataset") or {}
    return {
        "label": label, "status": "AVAILABLE" if output else "PARTIAL", "path": str(path),
        "source_sha256": sha256_file(path),
        "dataset": {"countries": dataset.get("countries"),
                    "industries": dataset.get("industries"),
                    "sectors": dataset.get("sectors"), "years": dataset.get("years"),
                    "sha256": dataset.get("sha256"), "units": dataset.get("units")},
        "scenarios": output,
        "note": ("quantity proxy from the existing AMFE comparative-static energy scenario; it is "
                 "NOT combined with the price model, NOT a forecast and NOT used to scale prices. "
                 "It is reported as an independent upper/lower reference for the demand-side "
                 "quantity effect"),
        "separation": "price_pass_through != quantity_effect",
    }


# ------------------------------------------------------------------------------------- report
def _digest(document: dict) -> str:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_report(data_root: Path | None = None, *, datasets=("figaro", "oecd_2025"),
                 pass_throughs=DEFAULT_PASS_THROUGH, out_dir: Path | None = None,
                 generated_at: str | None = None) -> dict:
    """Run the bridge for the requested datasets; returns the report and the written paths."""
    root = resolve_data_root(data_root)
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    oil = run_oil_scenario(DEFAULT_SCENARIO)
    dataset_paths = {"figaro": root / FIGARO_DATASET, "oecd_2025": root / OECD_2025_DATASET}
    classifications = {"figaro": "figaro", "oecd_2025": "oecd_2025"}
    entries = {}
    blocked = []
    for name in datasets:
        if name not in dataset_paths:
            raise BridgeError(f"unknown dataset {name!r}; known: {sorted(dataset_paths)}")
        path = dataset_paths[name]
        if not path.is_file():
            blocked.append({"dataset": name, "path": str(path), "reason": "dataset not found"})
            entries[name] = {"status": "BLOCKED_DATA", "reason": f"dataset not found: {path}"}
            continue
        matrix = load_dataset_matrix(path)
        variants = {}
        for pass_through in pass_throughs:
            variants[str(pass_through)] = run_price_model(
                matrix, classification=classifications[name], oil=oil,
                pass_through=float(pass_through), bucket_shares=DEFAULT_BUCKET_SHARES)
        # linearity check: Δp(pt) = pt / pt0 * Δp(pt0); compare via the aggregate index
        aggregates = {key: value["results"]["aggregate_price_index_pct"]
                      for key, value in variants.items()}
        reference = str(float(pass_throughs[0]))
        linear = all(abs(aggregates[key] - aggregates[reference] * (float(key) / float(reference)))
                     <= 1e-6 * max(1.0, abs(aggregates[key])) for key in aggregates)
        checks = {"linear_in_pass_through": bool(linear),
                  "pass_through_levels": [float(value) for value in pass_throughs],
                  "all_bridge_pass": all(value["checks"]["bridge_pass"]
                                         for value in variants.values())}
        entries[name] = {
            "status": "OK" if checks["all_bridge_pass"] else "FAIL",
            "dataset": {"path": str(path), "sha256": matrix["sha256"], "bytes": matrix["bytes"],
                        "countries": len(matrix["countries"]),
                        "industries": len(matrix["industries"]), "sectors": matrix["n"],
                        "years": matrix["years"], "units": matrix["units"],
                        "classification": classifications[name]},
            "variants": variants,
            "checks": checks,
        }
    report = {
        "schema": SCHEMA,
        "status": STATUS,
        "generated_at": generated_at or now(),
        "admission": False,
        "statement": ("Мост «цена → MRIO» — исследовательский расчёт pass-through цен "
                      "(Леонтьев). Это НЕ прогноз, НЕ оценка падения выпуска, НЕ допуск и НЕ "
                      "инвестиционная рекомендация. Количественный эффект приводится отдельно и "
                      "не смешивается с ценовым."),
        "market_shock": {
            "scenario": oil["scenario_id"], "module": oil["module_id"],
            "units": oil["units"], "horizon_months": oil["horizon_months"],
            "base_price_usd_bbl": oil["base_price"],
            "price_peak_usd_bbl": oil["price_peak"], "price_mean_usd_bbl": oil["price_mean"],
            "price_range_usd_bbl": [oil["price_low"], oil["price_high"]],
            "brent_delta_usd": oil["brent_delta_usd"],
            "brent_delta_pct": oil["brent_delta_pct"],
            "status": oil["status"], "digest": oil["digest"],
            "assumptions": oil["assumptions"],
        },
        "bridge_model": {
            "formula": "Δp = (I - A^T)^-1 c; c_i = pass_through * Δbrent% * sum_b share_b * s_{i,b}",
            "A": "A = Z / x (fixed intermediate-input coefficients of the canonical dataset)",
            "s_{i,b}": "share of cost bucket b in industry i's total intermediate cost, from A",
            "bucket_shares": dict(DEFAULT_BUCKET_SHARES),
            "bucket_rules": BUCKET_RULES,
            "assumptions": [
                "постоянная отдача от масштаба и фиксированная технология (A не меняется)",
                "полный перенос изменений удельных издержек в цены (full pass-through)",
                "цены относительные (базовый уровень = 1), без инфляционной динамики",
                "дефлятор добавленной стоимости не меняется; налоги/субсидии не моделируются",
                "шок нефти действует через корзины energy/transport/manufacturing/services, "
                "доли нефтяных издержек в корзинах — bridge-допущение (docs/147)",
                "нет замещения факторов, нет изменения спроса, нет обратных связей с выпуском",
            ],
        },
        "price_results": entries,
        "quantity_proxy": {
            "figaro_2019": quantity_proxy(ROOT / "build" / "world" / "report_index.json",
                                          "FIGARO 2019"),
            "oecd_2025": quantity_proxy(ROOT / "build" / "world_oecd_2025" / "report_index.json",
                                        "OECD ICIO 2025 (2019)"),
            "note": ("количественный прокси берётся из существующих сценарных отчётов AMFE и "
                     "приводится отдельным блоком; он не используется в ценовой модели и не "
                     "является прогнозом"),
        },
        "checks": {
            "datasets_requested": list(datasets),
            "datasets_ok": sorted(name for name, entry in entries.items()
                                  if entry.get("status") == "OK"),
            "blocked": blocked,
            "price_model_pass": all(entry.get("status") == "OK" for entry in entries.values()),
            "oil_module_research_only": oil["status"] == "RESEARCH_ONLY",
            "no_quantity_claims_in_price_model": True,
        },
        "limitations": [
            "только ценовой pass-through; никакого вывода о падении выпуска, занятости или ВВП",
            "количественный эффект — отдельный существующий сценарий energy+50% (прокси)",
            "фиксированная A и полный pass-through — верхняя граница переноса издержек",
            "корзины и доли нефтяных издержек — bridge-допущение, не наблюдаемые величины",
            "цены относительные и сравнительно-статические: без динамики, инфляции и ожиданий",
            "данные MRIO сторонние: в git не коммитятся, в отчёте только SHA-256 и агрегаты",
        ],
        "seconds": round(time.monotonic() - started, 1),
    }
    report_path = out_dir / "bridge_report.json"
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    # Canonical write (task-69): Path.write_text() translates "\n" to CRLF on Windows, so the file
    # digest stopped matching the digest of the in-memory payload.  Bytes are written as-is, in
    # UTF-8, with LF line endings on every platform.
    report_path.write_bytes(payload.encode("utf-8"))
    markdown_path = out_dir / "bridge_summary.md"
    markdown_path.write_bytes(render_markdown(report).encode("utf-8"))
    return {"report": report, "report_path": str(report_path),
            "summary_path": str(markdown_path),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": report_path.stat().st_size}


def render_markdown(report: dict) -> str:
    lines = ["# Мост «цена нефти → MRIO» (Леонтьев-цены)", "",
             f"Статус: **RESEARCH_ONLY**, admission=false, дата {report['generated_at']}.", "",
             "## Рыночный шок", "",
             f"- Brent: база {report['market_shock']['base_price_usd_bbl']:.2f} USD/bbl, "
             f"пик {report['market_shock']['price_peak_usd_bbl']:.2f}, "
             f"среднее {report['market_shock']['price_mean_usd_bbl']:.2f}; "
             f"Δ = +{report['market_shock']['brent_delta_usd']:.2f} USD/bbl "
             f"(+{100 * report['market_shock']['brent_delta_pct']:.2f}%)", ""]
    for name, entry in report["price_results"].items():
        if entry.get("status") != "OK":
            lines.append(f"## {name}: BLOCKED ({entry.get('reason')})")
            continue
        variants = entry["variants"]
        reference = variants.get("0.6") or list(variants.values())[0]
        results = reference["results"]
        lines += [f"## {name}: {entry['dataset']['countries']} x "
                  f"{entry['dataset']['industries']} = {entry['dataset']['sectors']} секторов",
                  "",
                  f"- ρ(A) = {reference['checks']['spectral_radius']:.12f}; "
                  f"productivity {reference['checks']['productive']}; "
                  f"Neumann {reference['checks']['neumann_converged']}",
                  f"- Агрегатный индекс цен: "
                  f"+{results['aggregate_price_index_pct']:.4f}% (output-weighted; "
                  f"pass-through {reference['pass_through']})",
                  f"- Макс Δp: {results['max_delta_price_pct']:.3f}%, "
                  f"медиана {results['median_delta_price_pct']:.3f}%", "",
                  "| Отрасль | Δp, % |", "|---|---|"]
        lines += [f"| {item['name']} | {item['delta_price_pct']:.3f} |"
                  for item in results["top_industries"][:8]]
        lines += ["", "| Страна | Δp, % |", "|---|---|"]
        lines += [f"| {item['name']} | {item['delta_price_pct']:.3f} |"
                  for item in results["top_countries"][:8]]
        lines += ["", "| pass-through | агрегатный Δp, % |", "|---|---|"]
        lines += [f"| {key} | {value['results']['aggregate_price_index_pct']:.4f} |"
                  for key, value in variants.items()]
        lines.append("")
    proxy = report["quantity_proxy"]
    lines += ["## Quantity-proxy (отдельно, не смешивается с ценами)", ""]
    for key in ("figaro_2019", "oecd_2025"):
        block = proxy.get(key) or {}
        scenario = (block.get("scenarios") or {}).get("energy_plus50")
        if scenario:
            lines.append(f"- {block.get('label')}: energy_plus50 Δвыпуск = "
                         f"{scenario['total_delta_output_usd_mn']:,.1f} USD млн "
                         f"({100 * scenario['relative_delta_output']:+.4f}%) — количественный "
                         f"прокси, не прогноз")
    lines += ["", "## Ограничения", ""] + [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Oil price -> MRIO Leontief price bridge")
    parser.add_argument("--data-root", default=None,
                        help="MRIO data root (default AMFE_MRIO_ROOT, else F:, else repo/data)")
    parser.add_argument("--dataset", default="all", choices=("all", "figaro", "oecd_2025"))
    parser.add_argument("--pass-through", dest="pass_through", default="0.3,0.6,1.0",
                        help="comma-separated pass-through levels in [0, 1]")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--generated-at", default=None, help="ISO timestamp (determinism)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    datasets = ("figaro", "oecd_2025") if args.dataset == "all" else (args.dataset,)
    try:
        pass_throughs = tuple(float(value) for value in str(args.pass_through).split(",")
                              if value.strip())
        if not pass_throughs or any(not 0.0 <= value <= 1.0 for value in pass_throughs):
            raise BridgeError("pass-through levels must be in [0, 1]")
        outcome = build_report(args.data_root, datasets=datasets,
                               pass_throughs=pass_throughs, out_dir=Path(args.out),
                               generated_at=args.generated_at)
    except BridgeError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    summary = {"status": "OK", "schema": outcome["report"]["schema"],
               "report": outcome["report_path"], "summary": outcome["summary_path"],
               "sha256": outcome["sha256"], "bytes": outcome["bytes"],
               "checks": outcome["report"]["checks"],
               "datasets": {name: entry.get("status")
                            for name, entry in outcome["report"]["price_results"].items()}}
    if args.json:
        print(json.dumps(outcome["report"], ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
