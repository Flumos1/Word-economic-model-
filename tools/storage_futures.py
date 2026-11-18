"""CLI: storage + futures curve for the oil layer (M8-A, docs/177 / docs/180).

Commands
--------
    python tools/storage_futures.py                    # report JSON + Markdown into build/storage_futures/
    python tools/storage_futures.py --json             # print the report as JSON (no files)
    python tools/storage_futures.py --month 2020-04    # observed EIA month used as the spot S
    python tools/storage_futures.py --spot 40 --cover 45
    python tools/storage_futures.py --quiet            # write files, print one line

Inputs: the committed, hash-pinned monthly Brent series (data/oil/brent_monthly_eia.csv, EIA RBRTE,
public domain) for the observed spot, and declared scenario assumptions for stocks, demand, rates
and storage cost.  Outputs: `report.json` (aggregates and short paths only), `report.md`,
`params.json` and `params.sha256.txt`.  Nothing is written into `data/` or `src/`, no raw archive is
stored, and the run is deterministic (same inputs -> same bytes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets.contract import MarketError  # noqa: E402
from amfe.markets.storage import (  # noqa: E402
    RESEARCH_ONLY,
    DEFAULT_PARAMS,
    StorageError,
    build_report,
    load_spot,
    report_markdown,
)
from amfe.markets.oil_v3 import load_series  # noqa: E402

DEFAULT_OUT = ROOT / "build" / "storage_futures"


def _write(path: Path, payload: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    return {"path": str(path).replace("\\", "/"), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def run(args) -> dict:
    series = load_series(Path(args.series) if args.series else None)
    report = build_report(series, DEFAULT_PARAMS, spot=args.spot, month=args.month,
                          stock_mb=args.stock, cover_days=args.cover,
                          demand_mb_per_day=args.demand, spr_mb=args.spr)
    document = report.as_dict()
    document["status"] = RESEARCH_ONLY
    written = {}
    out_dir = Path(args.out)
    if not args.json:
        written["report_json"] = _write(
            out_dir / "report.json",
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        written["report_md"] = _write(out_dir / "report.md", report_markdown(report))
        written["params_json"] = _write(
            out_dir / "params.json",
            json.dumps(DEFAULT_PARAMS.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n")
        written["params_sha256"] = _write(out_dir / "params.sha256.txt",
                                          DEFAULT_PARAMS.digest() + "\n")
    return {"report": document, "report_obj": report, "written": written, "data": series,
            "spot": load_spot(series, args.month)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Storage + futures-curve layer for the oil market module (research-only, no "
                    "admission)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--series", default=None,
                        help="path to the monthly Brent CSV "
                             "(default data/oil/brent_monthly_eia.csv)")
    parser.add_argument("--month", default=None,
                        help="observed EIA month used as the spot (default: last published)")
    parser.add_argument("--spot", type=float, default=None,
                        help="explicit spot override in USD/bbl (source_class=assumption)")
    parser.add_argument("--stock", type=float, default=None,
                        help="starting total stock in mb (commercial + strategic)")
    parser.add_argument("--cover", type=float, default=None,
                        help="starting cover in days (converted to stock with --demand)")
    parser.add_argument("--demand", type=float, default=100.0,
                        help="served demand in mb/d used for the cover convention (default 100)")
    parser.add_argument("--spr", type=float, default=0.0,
                        help="strategic share of the starting stock in mb (default 0)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--quiet", action="store_true", help="write files, print one line")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (MarketError, StorageError) as error:
        print(f"storage/futures error: {error}", file=sys.stderr)
        return 2
    document = result["report"]
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    curve = document["curve"]
    data = document["data"]
    if args.quiet:
        print(f"{curve['regime']} | spot {curve['spot_usd_per_bbl']:.2f} | cover "
              f"{curve['cover_days']:.2f} d (норма {curve['normal_cover_days']:.2f}) | кэрри "
              f"{curve['carry_annual']:+.4f}/год | спред 6м "
              f"{curve['six_month_spread_usd']:+.2f} | "
              f"{result['written']['report_md']['path']}")
        return 0
    print("Запасы и фьючерсная кривая (M8-A, блок 8) — RESEARCH_ONLY, не допуск и не прогноз")
    print(f"  данные: {data['series_id']} {data['spot_month']} = "
          f"{data['spot_usd_per_bbl']:.2f} USD/bbl ({data['observations']} наблюдений, SHA-256 "
          f"{str(data['sha256'])[:16]}…, raw_data_committed={data['raw_data_committed']})")
    print(f"  покрытие: {curve['cover_days']:.2f} дней при норме {curve['normal_cover_days']:.2f} "
          f"(выводится из r+c); загрузка ёмкости {curve['capacity_utilisation']:.1%}")
    print(f"  y(cover) = {curve['convenience_yield']:.4f}/год, кэрри = "
          f"{curve['carry_annual']:+.4f}/год ({curve['carry_annual'] / 12.0:+.4f}/мес.) -> "
          f"{curve['regime']}")
    print("  кривая F(T) = S·exp((r + c − y(cover))·T/12):")
    for point in curve["points"]:
        print(f"    T={point['months']:>4g} мес.  F = {point['price']:8.2f}  (F/S "
              f"{point['basis']:.6f})")
    print(f"  спред 6 мес.: {curve['six_month_spread_usd']:+.2f} USD/bbl; невязки "
          f"{curve['residuals']['multiplicative_relative']:.2e} / "
          f"{curve['residuals']['forward_rate_absolute']:.2e} (допуск "
          f"{curve['residuals']['tolerance']:.0e})")
    print("  эпизоды:")
    for item in document["episodes"]:
        print(f"    {item['episode_id']:<22} {item['required_months']}+ "
              f"{item['required_regime']:<13} совпало {item['matching_months']}/"
              f"{item['months']} -> {item['verdict']}")
    print("  кросс-проверка с нефтяным модулем (docs/177 A5, +-10%):")
    for name, item in document["cross_check"]["comparisons"].items():
        ratio = "—" if item["ratio"] is None else f"{item['ratio']:.4f}"
        print(f"    {name:<28} ratio {ratio} (в полосе: "
              f"{'да' if item['within_band'] else 'нет'})")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
