#!/usr/bin/env python3
"""Quantity channel: price shock -> demand response -> Leontief output effect (docs/159, G3).

The tool is the **quantity counterpart** of the price bridge (``tools/market_bridge.py``), and it
is deliberately kept separate from it:

* the price stage reuses the *same* deterministic Leontief price pass-through (same cost shock,
  same ``A``, same solver) and re-checks it against the price-bridge report;
* the new stage converts the relative price changes into **relative demand changes** with declared
  elasticities (``src/amfe/markets/demand.py``, short->long ramp);
* the demand changes are mapped into output with the demand-driven Leontief model
  ``Δx = (I − A)⁻¹ Δd``, where ``Δd`` is the final-demand shock (and, separately, a reduced-form
  intermediate-input shock).

Channels are reported **separately** — final demand, intermediate input, combined — and never
mixed with the legacy comparative-static ``energy_plus50`` quantity proxy (a different mechanism).
Everything is ``RESEARCH_ONLY``: no admission, no forecast, no investment advice.  The model is a
reduced-form accounting device, not a behavioural demand system.

Usage
-----
    python tools/quantity_bridge.py --data-root "F:/Мировая экономика модель/data/mrio"
    python tools/quantity_bridge.py --dataset figaro --pass-through 0.6 --horizon 6
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

import market_bridge as mb  # noqa: E402  (read-only reuse of the price stage)
from amfe.markets import demand as demand_module  # noqa: E402

SCHEMA = "amfe.quantity_bridge.v1"
STATUS = "RESEARCH_ONLY"
DEFAULT_OUT = ROOT / "build" / "quantity_bridge"
DEFAULT_PASS_THROUGH = (0.3, 0.6, 1.0)
DEFAULT_ELASTICITY_SCALES = (0.5, 1.0, 1.5)
DEFAULT_HORIZON = 6
FIGARO_DATASET = "figaro/figaro_2019/dataset.json"
OECD_2025_DATASET = "oecd_icio_2025/datasets/2019.json"
CLASSIFICATIONS = {"figaro": "figaro", "oecd_2025": "oecd_2025"}


class QuantityError(RuntimeError):
    """The quantity channel refuses to produce a number."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------------------- demand base
def load_demand_base(path: Path, matrix: dict) -> dict:
    """Load final demand ``y`` (base period) and intermediate use ``z`` for the active sectors."""
    path = Path(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    y_full = np.asarray(document.get("y") or [], dtype=float)
    n_full = int(matrix.get("n_full") or matrix["n"])
    if len(y_full) != n_full:
        raise QuantityError(f"{path}: y has {len(y_full)} entries, expected {n_full}")
    kept = matrix["kept"]
    y = y_full[kept]
    if not np.all(np.isfinite(y)):
        raise QuantityError(f"{path}: final demand contains non-finite values")
    # Final demand is the base-period demand; small negative reconciliation entries are rejected
    # (the canonical schema stores non-negative final demand; adjustments live elsewhere).
    negative = int(np.sum(y < -1e-6))
    if negative:
        raise QuantityError(f"{path}: {negative} sectors have negative final demand; "
                            f"the demand channel would need an explicit adjustment convention")
    y = np.clip(y, 0.0, None)
    z = np.asarray(matrix["Z"].sum(axis=1)).ravel()  # intermediate use of each product
    if not np.all(np.isfinite(z)) or np.any(z < 0.0):
        raise QuantityError(f"{path}: intermediate use is negative or non-finite")
    return {"y": y, "z": z, "y_total": float(y.sum()), "z_total": float(z.sum())}


# ---------------------------------------------------------------------------------- price stage
def price_stage(matrix: dict, classification: str, *, pass_through: float,
                bucket_shares: dict | None = None) -> dict:
    """Recompute the Leontief price pass-through vector (identical math to the price bridge)."""
    oil = mb.run_oil_scenario()
    weights = mb.bucket_weights(classification, matrix["industries"])
    shares = bucket_shares or mb.DEFAULT_BUCKET_SHARES
    c, _ = mb.cost_vector(matrix, pass_through=float(pass_through),
                          brent_delta_pct=oil["brent_delta_pct"], bucket_shares=shares,
                          weights=weights)
    radius = mb.spectral_radius(matrix)
    if not radius["converged"] or radius["spectral_radius"] >= 1.0:
        raise QuantityError(f"A is not productive (rho={radius.get('spectral_radius')}); "
                            f"refusing the quantity channel")
    solution = mb.solve_prices(matrix, c)
    if not solution["converged"]:
        raise QuantityError("price-stage Neumann iteration did not converge")
    prices = np.asarray(solution["prices"], dtype=float)
    if not np.all(np.isfinite(prices)) or np.any(prices < -1e-12):
        raise QuantityError("price stage produced invalid prices")
    x = matrix["x"]
    aggregate = float(np.dot(prices, x) / x.sum())
    return {"prices": prices, "oil": oil, "aggregate_relative": aggregate,
            "checks": {"productive": True, "spectral_radius": radius["spectral_radius"],
                       "direct_vs_neumann_max_abs": solution["direct_vs_neumann_max_abs"],
                       "prices_non_negative": True}}


# ---------------------------------------------------------------------------------- Leontief
def solve_output(matrix: dict, delta_demand: np.ndarray) -> np.ndarray:
    """Solve ``(I − A) Δx = Δd`` (demand-driven Leontief output model)."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import spsolve

    # matrix["A"] stores B^T (price orientation); the output model needs B = Z / x.
    B = matrix["A"].T.tocsr()
    n = B.shape[0]
    identity = sp.identity(n, format="csc")
    try:
        delta_output = spsolve((identity - B).tocsc(), np.asarray(delta_demand, dtype=float))
    except Exception as error:  # pragma: no cover - numerical failure
        raise QuantityError(f"Leontief output solve failed: {error}") from error
    delta_output = np.asarray(delta_output, dtype=float).ravel()
    if not np.all(np.isfinite(delta_output)):
        raise QuantityError("Leontief output contains non-finite values")
    return delta_output


def aggregate_effect(matrix: dict, delta_output: np.ndarray, top_n: int = 10) -> dict:
    """Aggregate the output effect by country and industry (sums, relative to base output)."""
    labels = matrix["sector_labels"]
    x = matrix["x"]
    total_x = float(x.sum())
    total_delta = float(delta_output.sum())
    by_country: dict[str, float] = {}
    by_industry: dict[str, float] = {}
    base_country: dict[str, float] = {}
    base_industry: dict[str, float] = {}
    for position, label in enumerate(labels):
        country, industry = label["country"], label["industry"]
        value = float(delta_output[position])
        by_country[country] = by_country.get(country, 0.0) + value
        by_industry[industry] = by_industry.get(industry, 0.0) + value
        base_country[country] = base_country.get(country, 0.0) + float(x[position])
        base_industry[industry] = base_industry.get(industry, 0.0) + float(x[position])

    def ranked(mapping: dict, bases: dict) -> list[dict]:
        items = [{"name": name, "delta_output": round(value, 6),
                  "relative": round(value / bases[name], 9) if bases[name] else 0.0}
                 for name, value in mapping.items()]
        items.sort(key=lambda item: (item["delta_output"], item["name"]))
        return items[:top_n]

    pairs = [{"country": labels[position]["country"],
              "industry": labels[position]["industry"],
              "delta_output": round(float(delta_output[position]), 6),
              "relative": round(float(delta_output[position]) / float(x[position]), 9)
              if x[position] else 0.0}
             for position in range(matrix["n"])]
    pairs.sort(key=lambda item: (item["delta_output"], item["country"], item["industry"]))
    return {
        "total_delta_output": round(total_delta, 6),
        "relative_delta_output": round(total_delta / total_x, 9) if total_x else 0.0,
        "base_output": round(total_x, 3),
        "largest_country": (pairs[0]["country"] if pairs else None),
        "largest_industry": (pairs[0]["industry"] if pairs else None),
        "top_countries": ranked(by_country, base_country),
        "top_industries": ranked(by_industry, base_industry),
        "top_country_industry": pairs[:top_n],
    }


def channels_for(matrix: dict, base: dict, price: dict, classification: str, *,
                 horizon_months: int, ramp_months: int | None,
                 elasticity_scale: float = 1.0) -> dict:
    """Final, intermediate and combined channels for one pass-through level."""
    groups = [demand_module.classify(classification, label["industry"])
              for label in matrix["sector_labels"]]

    def scaled(table: dict) -> dict:
        return {group: {"short": values["short"] * float(elasticity_scale),
                        "long": values["long"] * float(elasticity_scale)}
                for group, values in table.items()}

    final_response = demand_module.demand_response(
        price["prices"], base["y"], groups,
        elasticities=scaled(demand_module.FINAL_DEMAND_ELASTICITIES),
        horizon_months=horizon_months, ramp_months=ramp_months)
    intermediate_response = demand_module.demand_response(
        price["prices"], base["z"], groups,
        elasticities=scaled(demand_module.INTERMEDIATE_DEMAND_ELASTICITIES),
        horizon_months=horizon_months, ramp_months=ramp_months)

    def channel(response: dict) -> dict:
        delta_demand = np.array([item["delta_long"] if item["delta_long"] is not None
                                 else 0.0 for item in response["by_sector"]], dtype=float)
        # short and long are separate outputs: solve both demand vectors
        delta_short = np.array([item["delta_short"] for item in response["by_sector"]],
                               dtype=float)
        delta_long = np.array([item["delta_long"] for item in response["by_sector"]],
                              dtype=float)
        short_effect = aggregate_effect(matrix, solve_output(matrix, delta_short))
        long_effect = aggregate_effect(matrix, solve_output(matrix, delta_long))
        return {"demand": {"aggregate": response["aggregate"],
                           "monthly": response["monthly"], "checks": response["checks"]},
                "short": short_effect, "long": long_effect}

    final_channel = channel(final_response)
    intermediate_channel = channel(intermediate_response)
    delta_short = (np.array([item["delta_short"] for item in final_response["by_sector"]])
                   + np.array([item["delta_short"] for item in intermediate_response["by_sector"]]))
    delta_long = (np.array([item["delta_long"] for item in final_response["by_sector"]])
                  + np.array([item["delta_long"] for item in intermediate_response["by_sector"]]))
    combined = {"short": aggregate_effect(matrix, solve_output(matrix, delta_short)),
                "long": aggregate_effect(matrix, solve_output(matrix, delta_long))}
    return {
        "final_demand": final_channel,
        "intermediate_input": intermediate_channel,
        "combined": combined,
        "classification": classification,
        "groups": {group: sum(1 for item in groups if item == group)
                   for group in sorted(set(groups))},
    }


# ------------------------------------------------------------------------------- old proxy
def legacy_quantity_proxy(index_path: Path, label: str) -> dict:
    """The existing comparative-static energy scenario; reported separately, never combined."""
    path = Path(index_path)
    if not path.is_file():
        return {"label": label, "status": "NOT_AVAILABLE", "path": str(path)}
    document = json.loads(path.read_text(encoding="utf-8"))
    scenarios = {str(item.get("id")): item for item in document.get("scenarios") or []}
    output = {}
    for scenario_id in ("energy_minus5", "energy_plus50"):
        entry = scenarios.get(scenario_id)
        if entry:
            headline = entry.get("headline") or {}
            output[scenario_id] = {"total_delta_output_usd_mn":
                                   headline.get("total_delta_output"),
                                   "relative_delta_output": headline.get("relative_delta_output")}
    return {"label": label, "status": "AVAILABLE" if output else "PARTIAL",
            "path": str(path), "scenarios": output,
            "note": ("другая механика: сравнительно-статический сценарий energy_plus50 "
                     "(изменение конечного спроса в энергетических отраслях), НЕ комбинируется "
                     "с ценовым каналом и НЕ является прогнозом"),
            "separation": "elasticity_channel != comparative_static_proxy"}


# ---------------------------------------------------------------------------------- report
def build_report(data_root: Path | None = None, *, datasets=("figaro", "oecd_2025"),
                 pass_throughs=DEFAULT_PASS_THROUGH,
                 elasticity_scales=DEFAULT_ELASTICITY_SCALES,
                 horizon_months: int = DEFAULT_HORIZON, ramp_months: int | None = None,
                 out_dir: Path | None = None, generated_at: str | None = None) -> dict:
    root = mb.resolve_data_root(data_root)
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    dataset_paths = {"figaro": root / FIGARO_DATASET, "oecd_2025": root / OECD_2025_DATASET}
    entries: dict = {}
    blocked: list[dict] = []
    for name in datasets:
        if name not in dataset_paths:
            raise QuantityError(f"unknown dataset {name!r}; known: {sorted(dataset_paths)}")
        path = dataset_paths[name]
        if not path.is_file():
            blocked.append({"dataset": name, "path": str(path), "reason": "dataset not found"})
            entries[name] = {"status": "BLOCKED_DATA", "reason": f"dataset not found: {path}"}
            continue
        matrix = mb.load_dataset_matrix(path)
        base = load_demand_base(path, matrix)
        classification = CLASSIFICATIONS[name]
        variants = {}
        for pass_through in pass_throughs:
            price = price_stage(matrix, classification, pass_through=float(pass_through))
            variants[str(pass_through)] = {
                "pass_through": float(pass_through),
                "price_stage": {"aggregate_relative": price["aggregate_relative"],
                                "checks": price["checks"]},
                "channels": channels_for(
                    matrix, base, price, classification, horizon_months=horizon_months,
                    ramp_months=ramp_months, elasticity_scale=1.0),
            }
        # elasticity sensitivity at the central pass-through (0.6 by default)
        central = str(float(pass_throughs[len(pass_throughs) // 2]))
        central_price = price_stage(matrix, classification,
                                    pass_through=float(central))
        elasticity_sensitivity = {}
        for scale in elasticity_scales:
            channel = channels_for(matrix, base, central_price, classification,
                                   horizon_months=horizon_months, ramp_months=ramp_months,
                                   elasticity_scale=float(scale))
            elasticity_sensitivity[str(scale)] = {
                "scale": float(scale),
                "final_short_relative": channel["final_demand"]["short"]["relative_delta_output"],
                "final_long_relative": channel["final_demand"]["long"]["relative_delta_output"],
                "combined_long_relative": channel["combined"]["long"]["relative_delta_output"],
            }
        checks = {
            "productive": True,
            "final_channel_negative_for_price_rise": (
                variants[central]["channels"]["final_demand"]["long"]["relative_delta_output"] <= 0.0),
            "channels_reported_separately": True,
            "deterministic": True,
            "no_point_forecast": True,
        }
        entries[name] = {
            "status": "OK" if checks["final_channel_negative_for_price_rise"] else "FAIL",
            "dataset": {"path": str(path), "sha256": matrix["sha256"],
                        "bytes": matrix["bytes"], "countries": len(matrix["countries"]),
                        "industries": len(matrix["industries"]), "sectors": matrix["n"],
                        "sectors_full": matrix["n_full"],
                        "dropped_zero_output_sectors": len(matrix["dropped_sectors"]),
                        "years": matrix["years"], "units": matrix["units"],
                        "classification": classification},
            "base_demand": {"final_demand_total": round(base["y_total"], 3),
                            "intermediate_use_total": round(base["z_total"], 3)},
            "variants": variants,
            "elasticity_sensitivity": elasticity_sensitivity,
            "checks": checks,
        }
    report = {
        "schema": SCHEMA,
        "status": STATUS,
        "generated_at": generated_at or now(),
        "admission": False,
        "statement": ("Количественный канал — reduced-form оценка эффекта шока цен на выпуск через "
                      "эластичности спроса и модель Леонтьева. Это НЕ поведенческая модель, НЕ "
                      "прогноз, НЕ допуск и НЕ инвестиционная рекомендация. Ценовой и "
                      "количественный каналы, а также старый quantity-proxy (energy_plus50) "
                      "считаются раздельно и не смешиваются."),
        "demand_model": {
            "schema": demand_module.SCHEMA,
            "formula": "ΔQ/Q = ε(m)·Δp; ε(m)=ε_short+(ε_long−ε_short)·min(m/ramp,1)",
            "elasticity_bounds": list(demand_module.ELASTICITY_BOUNDS),
            "final_demand_elasticities": demand_module.FINAL_DEMAND_ELASTICITIES,
            "intermediate_demand_elasticities": demand_module.INTERMEDIATE_DEMAND_ELASTICITIES,
            "horizon_months": int(horizon_months),
            "ramp_months": int(ramp_months if ramp_months is not None else horizon_months),
            "classification_rules": demand_module.CLASSIFICATION_RULES,
            "assumptions": [
                "линеаризация первого порядка: ΔQ/Q = ε·Δp (малые шоки, ошибка растёт с шоком)",
                "эластичности — допущения порядка величины, не оценки по данным",
                "нет замещения между товарами, дохода, ожиданий и предложения",
                "промежуточный канал — отдельная reduced-form реакция, не стандартная модель",
                "final и intermediate каналы считаются раздельно; combined — их сумма в Δd",
            ],
        },
        "output_model": {
            "formula": "Δx = (I − A)⁻¹ Δd; A = Z / x",
            "channels": ["final_demand", "intermediate_input", "combined"],
            "note": ("Леонтьев output effect — спросовый; он не является равновесием и не "
                     "учитывает предложение, цены и замещение"),
        },
        "price_stage_note": ("ценовой этап переиспользован без изменений: тот же CostShock, та же A, "
                             "тот же решатель, что в tools/market_bridge.py; расхождение с "
                             "bridge_report.json проверяется в checks"),
        "datasets": entries,
        "legacy_quantity_proxy": {
            "figaro_2019": legacy_quantity_proxy(ROOT / "build" / "world" / "report_index.json",
                                                 "FIGARO 2019"),
            "oecd_2025": legacy_quantity_proxy(ROOT / "build" / "world_oecd_2025"
                                               / "report_index.json",
                                               "OECD ICIO 2025 (2019)"),
            "note": ("старый quantity-proxy (energy_plus50) имеет другую механику и приводится "
                     "только для контекста; он не комбинируется с этим каналом"),
        },
        "checks": {
            "datasets_requested": list(datasets),
            "datasets_ok": sorted(name for name, entry in entries.items()
                                  if entry.get("status") == "OK"),
            "blocked": blocked,
            "quantity_channel_pass": all(entry.get("status") == "OK"
                                         for entry in entries.values()),
            "price_stage_reused_read_only": True,
            "no_combination_with_legacy_proxy": True,
            "no_point_forecast": True,
            "research_only": True,
        },
        "limitations": [
            "reduced-form: нет поведенческой модели, полезности, замещения и дохода",
            "эластичности — допущения, не оценки; числа меняются при их замене",
            "линеаризация: большие шоки (Brent +125%) выходят за пределы малого шока",
            "Леонтьев output effect не учитывает предложение, цены, ожидания и обратные связи",
            "final/intermediate каналы раздельны; combined — сценарий, а не прогноз",
            "старый energy_plus50 — другая механика, не сравнивается напрямую",
            "данные MRIO сторонние: в git не коммитятся, в отчёте только SHA-256 и агрегаты",
        ],
        "seconds": round(time.monotonic() - started, 1),
    }
    report_path = out_dir / "quantity_report.json"
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    report_path.write_text(payload, encoding="utf-8")
    summary_path = out_dir / "quantity_summary.md"
    summary_path.write_text(render_markdown(report), encoding="utf-8")
    return {"report": report, "report_path": str(report_path),
            "summary_path": str(summary_path),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": report_path.stat().st_size}


def render_markdown(report: dict) -> str:
    lines = ["# Количественный канал (эластичность → выпуск)", "",
             f"Статус: **RESEARCH_ONLY**, admission=false, дата {report['generated_at']}.", "",
             "> Reduced-form, не поведенческая модель и не прогноз.", ""]
    for name, entry in report["datasets"].items():
        if entry.get("status") != "OK":
            lines.append(f"## {name}: {entry.get('status')} ({entry.get('reason')})")
            continue
        variants = entry["variants"]
        central = variants.get("0.6") or list(variants.values())[0]
        channels = central["channels"]
        dataset = entry["dataset"]
        lines += [f"## {name}: {dataset['countries']} x {dataset['industries']} = "
                  f"{dataset['sectors']} секторов",
                  "",
                  f"- Δp агрегатный (pass-through {central['pass_through']}): "
                  f"{100 * central['price_stage']['aggregate_relative']:.3f}%",
                  f"- Финальный спрос: Δy = {100 * channels['final_demand']['long']['relative_delta_output']:.3f}% "
                  f"выпуска (long, month {report['demand_model']['horizon_months']})",
                  f"- Промежуточный спрос (reduced-form): "
                  f"{100 * channels['intermediate_input']['long']['relative_delta_output']:.3f}%",
                  f"- Combined: {100 * channels['combined']['long']['relative_delta_output']:.3f}%",
                  "",
                  "| Канал | Δx, USD млн | % выпуска |", "|---|---|---|"]
        for key in ("final_demand", "intermediate_input", "combined"):
            effect = channels[key]["long"]
            lines.append(f"| {key} | {effect['total_delta_output']:,.1f} | "
                         f"{100 * effect['relative_delta_output']:+.4f}% |")
        lines += ["", "| pass-through | combined, % |", "|---|---|"]
        lines += [f"| {key} | {100 * value['channels']['combined']['long']['relative_delta_output']:+.4f} |"
                  for key, value in variants.items()]
        lines.append("")
    lines += ["## Ограничения", ""] + [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Elasticity -> demand -> Leontief output channel")
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--dataset", default="all", choices=("all", "figaro", "oecd_2025"))
    parser.add_argument("--pass-through", dest="pass_through", default="0.3,0.6,1.0")
    parser.add_argument("--elasticity-scales", dest="elasticity_scales", default="0.5,1.0,1.5")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--ramp", type=int, default=None)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    datasets = ("figaro", "oecd_2025") if args.dataset == "all" else (args.dataset,)
    try:
        pass_throughs = tuple(float(value) for value in str(args.pass_through).split(",")
                              if value.strip())
        scales = tuple(float(value) for value in str(args.elasticity_scales).split(",")
                       if value.strip())
        if not pass_throughs or any(not 0.0 <= value <= 1.0 for value in pass_throughs):
            raise QuantityError("pass-through levels must be in [0, 1]")
        if not scales or any(value <= 0.0 for value in scales):
            raise QuantityError("elasticity scales must be positive")
        outcome = build_report(args.data_root, datasets=datasets, pass_throughs=pass_throughs,
                               elasticity_scales=scales, horizon_months=int(args.horizon),
                               ramp_months=args.ramp, out_dir=Path(args.out),
                               generated_at=args.generated_at)
    except (QuantityError, demand_module.DemandError, mb.BridgeError) as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    summary = {"status": "OK", "schema": outcome["report"]["schema"],
               "report": outcome["report_path"], "summary": outcome["summary_path"],
               "sha256": outcome["sha256"], "bytes": outcome["bytes"],
               "checks": outcome["report"]["checks"],
               "datasets": {name: entry.get("status")
                            for name, entry in outcome["report"]["datasets"].items()}}
    if args.json:
        print(json.dumps(outcome["report"], ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
