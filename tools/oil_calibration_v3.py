"""CLI: oil module v3 — real EIA monthly Brent, regime split, demand channel (docs/165).

Commands
--------
    python tools/oil_calibration_v3.py                  # protocol -> per-regime fit -> OOS report
    python tools/oil_calibration_v3.py --protocol-only  # print the pre-registered protocol + SHA-256
    python tools/oil_calibration_v3.py --json           # print the full report as JSON
    python tools/oil_calibration_v3.py --quiet          # write files, print one line

The tool reads the committed, hash-pinned monthly Brent series (data/oil/brent_monthly_eia.csv,
EIA RBRTE, public domain), builds the per-regime protocol, fits each regime on its train episodes
only and scores the held-out episodes with the frozen parameters.  It never re-tunes on OOS, never
writes into src/ and never claims an admission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets.oil_v3 import (  # noqa: E402
    RESEARCH_ONLY,
    OilV3Error,
    build_protocol,
    calibrate,
    load_series,
    protocol_sha256,
    report_markdown,
)

DEFAULT_OUT = ROOT / "build" / "oil_v3"


def _write(path: Path, payload: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    return {"path": str(path).replace("\\", "/"), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def run(args) -> dict:
    series = load_series(Path(args.series) if args.series else None)
    protocol = build_protocol()
    digest = protocol_sha256(protocol)
    out_dir = Path(args.out)
    written = {}
    if args.protocol_only:
        if not args.json:
            written["protocol"] = _write(
                out_dir / "protocol.json",
                json.dumps(protocol, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            written["protocol_sha256"] = _write(out_dir / "protocol.sha256.txt", digest + "\n")
        return {"protocol": protocol, "protocol_sha256": digest, "written": written,
                "report": None, "report_obj": None, "data": series}
    report = calibrate(series)
    document = report.as_dict()
    document["status"] = RESEARCH_ONLY
    if not args.json:
        written["protocol"] = _write(
            out_dir / "protocol.json",
            json.dumps(protocol, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        written["protocol_sha256"] = _write(out_dir / "protocol.sha256.txt", digest + "\n")
        written["report"] = _write(
            out_dir / "report.json",
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        written["markdown"] = _write(out_dir / "report.md", report_markdown(report))
    return {"protocol": protocol, "protocol_sha256": digest, "report": document,
            "report_obj": report, "written": written, "verdict": report.verdict,
            "data": series}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Oil module v3: real EIA monthly Brent, regime-specific calibration and OOS "
                    "(research-only, no admission)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--series", default=None,
                        help="path to the monthly Brent CSV (default data/oil/brent_monthly_eia.csv)")
    parser.add_argument("--protocol-only", action="store_true",
                        help="print the pre-registered protocol and SHA-256 (no fit, no OOS)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--quiet", action="store_true", help="write files, print one line")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except OilV3Error as error:
        print(f"oil v3 error: {error}", file=sys.stderr)
        return 2
    if args.protocol_only:
        if args.json:
            print(json.dumps({"protocol": result["protocol"],
                              "protocol_sha256": result["protocol_sha256"]},
                             ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(json.dumps(result["protocol"], ensure_ascii=False, sort_keys=True, indent=2))
            print(f"\nprotocol SHA-256: {result['protocol_sha256']}")
            print(f"data: {result['data']['path']} "
                  f"({result['data']['observations']} observations, "
                  f"SHA-256 {result['data']['sha256'][:16]}…)")
            for name, info in result["written"].items():
                print(f"{name}: {info['path']} ({info['bytes']} B)")
        return 0
    document = result["report"]
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    comparison = document["comparison"]
    params = document["fits"]["params"]
    if args.quiet:
        print(f"{document['verdict']} | acute {params['acute']} | prolonged "
              f"{params['prolonged']} | peak MAE {comparison['peak_change_mae_calibrated']:.2f} vs "
              f"{comparison['peak_change_mae_reference']:.2f} v1 | "
              f"{result['written']['markdown']['path']}")
        return 0
    print("Нефть v3: реальные месячные ряды, режимы, demand-канал (research-only)")
    print(f"  protocol SHA-256: {result['protocol_sha256']}")
    print(f"  данные: {result['data']['series_id']} {result['data']['first_month']}…"
          f"{result['data']['last_month']}, {result['data']['observations']} наблюдений, "
          f"SHA-256 {result['data']['sha256'][:16]}…")
    for regime in ("acute", "prolonged"):
        fit = document["fits"][regime]
        row = fit["params"]
        print(f"  {regime}: εshort={row['elasticity_short']}, εlong={row['elasticity_long']} "
              f"(train RMSE {fit['train']['mean_path_rmse']:.2f}, "
              f"{fit['candidates_evaluated']} точек)")
    print(f"  demand: εshort={params['demand']['elasticity_short']}, "
          f"εlong={params['demand']['elasticity_long']} (не обучено)")
    print(f"  verdict: {document['verdict']}")
    print(f"  OOS: MAE пути {comparison['mean_path_mae_calibrated']:.2f} против "
          f"{comparison['mean_path_mae_reference']:.2f} (v1); RMSE пути "
          f"{comparison['mean_path_rmse_calibrated']:.2f} против "
          f"{comparison['mean_path_rmse_reference']:.2f}; peak MAE "
          f"{comparison['peak_change_mae_calibrated']:.2f} против "
          f"{comparison['peak_change_mae_reference']:.2f}")
    print(f"  направление: {comparison['direction_calibrated']}/4 против "
          f"{comparison['direction_reference']}/4 (v1); порядок: "
          f"{comparison['order_calibrated']}/4 против {comparison['order_reference']}/4")
    print("  эпизоды OOS:")
    for row in document["oos"]["episodes"]:
        print(f"    {row['episode_id']:<16} {row['regime']:<10} факт {row['actual_peak_change']:+7.2f} "
              f"модель {row['model_peak_change']:+7.2f} (ошибка {row['peak_change_error']:+6.2f}) "
              f"ratio {row['peak_change_ratio'] if row['peak_change_ratio'] is not None else float('nan'):5.2f} "
              f"RMSE пути {row['path_rmse']:6.2f}")
    if document["where_worse"]:
        print(f"  хуже v1 по RMSE пути: {', '.join(document['where_worse'])}")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
