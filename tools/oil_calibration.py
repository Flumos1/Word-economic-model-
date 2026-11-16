"""CLI: PIT-style calibration and OOS check of the oil market module (docs/158).

Commands
--------
    python tools/oil_calibration.py                    # protocol -> fit -> OOS, write build/market_calibration/
    python tools/oil_calibration.py --protocol-only    # print the pre-registered protocol + SHA-256
    python tools/oil_calibration.py --json             # print the full report as JSON
    python tools/oil_calibration.py --quiet            # write files, print one line

The protocol (grid + selection rule + success criteria + train/OOS ids) is built and hashed *before*
the OOS scoring, and its SHA-256 is written next to the report.  The tool never reads third-party
data, never re-tunes on OOS and never claims an admission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets.calibration import (  # noqa: E402
    REPORT_VERSION,
    RESEARCH_ONLY,
    CalibrationError,
    build_protocol,
    calibrate,
    protocol_sha256,
    report_markdown,
)

DEFAULT_OUT = ROOT / "build" / "market_calibration"


def _write(path: Path, payload: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def run(args) -> dict:
    protocol = build_protocol()
    digest = protocol_sha256(protocol)
    out_dir = Path(args.out)
    if args.protocol_only:
        written = {}
        if not args.json:
            written["protocol"] = _write(
                out_dir / "protocol.json",
                json.dumps(protocol, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            written["protocol_sha256"] = _write(out_dir / "protocol.sha256.txt", digest + "\n")
        return {"protocol": protocol, "protocol_sha256": digest, "written": written,
                "report": None}
    report = calibrate()
    document = report.as_dict()
    document["status"] = RESEARCH_ONLY
    written = {}
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
            "written": written, "verdict": report.verdict}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate the oil module on train episodes and "
                                                 "score 1990/2019/2022 out of sample")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--protocol-only", action="store_true",
                        help="print the pre-registered protocol and SHA-256 (no fit, no OOS)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--quiet", action="store_true", help="write files, print one line")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except CalibrationError as error:
        print(f"calibration error: {error}", file=sys.stderr)
        return 1
    if args.protocol_only:
        if args.json:
            print(json.dumps({"protocol": result["protocol"],
                              "protocol_sha256": result["protocol_sha256"]},
                             ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(json.dumps(result["protocol"], ensure_ascii=False, sort_keys=True, indent=2))
            print(f"\nprotocol SHA-256: {result['protocol_sha256']}")
            for name, info in result["written"].items():
                print(f"{name}: {info['path']} ({info['bytes']} B)")
        return 0
    document = result["report"]
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    fit_result = document["fit"]
    params = fit_result["params"]
    comparison = document["comparison"]
    if args.quiet:
        print(f"{document['verdict']} | params εshort {params['elasticity_short']}, "
              f"εlong {params['elasticity_long']}, premium {params['risk_premium']} | "
              f"OOS MAE {comparison['mae_calibrated_usd']:.2f} vs fixed "
              f"{comparison['mae_fixed_usd']:.2f} | {result['written']['markdown']['path']}")
        return 0
    print("PIT-калибровка и OOS нефтяного модуля (research-only, не прогноз и не допуск)")
    print(f"  protocol SHA-256: {result['protocol_sha256']}")
    print(f"  grid: {document['protocol']['grid']['size']} точек; "
          f"train: {', '.join(document['protocol']['train_episodes'])}; "
          f"OOS: {', '.join(document['protocol']['oos_episodes'])}")
    print(f"  выбрано на train: εshort={params['elasticity_short']}, "
          f"εlong={params['elasticity_long']}, risk premium={params['risk_premium']} USD/bbl "
          f"(train MAE {fit_result['train']['mae_usd']:.2f}, "
          f"RMSE {fit_result['train']['rmse_usd']:.2f})")
    print(f"  verdict: {document['verdict']}")
    print(f"  OOS MAE: калиброванная {comparison['mae_calibrated_usd']:.2f} против фиксированной "
          f"{comparison['mae_fixed_usd']:.2f} (Δ {comparison['mae_delta_usd']:+.2f})")
    print(f"  направление: {comparison['direction_calibrated']}/3 (фикс. "
          f"{comparison['direction_fixed']}/3); порядок: {comparison['order_calibrated']}/3 "
          f"(фикс. {comparison['order_fixed']}/3)")
    print("  эпизоды OOS:")
    for row, reference in zip(document["oos_calibrated"]["episodes"],
                              document["oos_fixed_reference"]["episodes"]):
        print(f"    {row['episode_id']:<12} факт {row['actual_delta_usd']:+.1f}  "
              f"калибр {row['model_delta_usd']:+.2f} (ошибка {row['error_usd']:+.2f})  "
              f"фикс {reference['model_delta_usd']:+.2f} (ошибка {reference['error_usd']:+.2f})  "
              f"ratio {row['ratio']:.2f}  {'OK' if row['direction_ok'] else 'FAIL'}/"
              f"{'OK' if row['order_ok'] else 'FAIL'}")
    if document["where_worse"]:
        print(f"  хуже фиксированной: {', '.join(document['where_worse'])}")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
