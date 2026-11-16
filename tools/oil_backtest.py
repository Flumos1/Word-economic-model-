"""CLI: historical backtest of the oil market module (docs/155).

Commands
--------
    python tools/oil_backtest.py                       # run all episodes, write build/market_backtest/
    python tools/oil_backtest.py --json                 # print the report as JSON (no files)
    python tools/oil_backtest.py --episodes 1990_gulf,2022_russia --out build/market_backtest
    python tools/oil_backtest.py --quiet                # only write the files

The tool writes ``report.json`` (machine-readable, aggregate-only) and ``report.md`` (table) into
``build/market_backtest`` (git-ignored).  It never reads or writes third-party data and never
claims an admission: the verdict is the mechanical one from :mod:`amfe.markets.backtest`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets.backtest import (  # noqa: E402
    EPISODES,
    RESEARCH_ONLY,
    BacktestError,
    backtest,
    report_markdown,
    RESULT_SCHEMA,
)

DEFAULT_OUT = ROOT / "build" / "market_backtest"


def _write(path: Path, payload: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    import hashlib

    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def run(args) -> dict:
    episode_ids = None
    if args.episodes:
        episode_ids = [item.strip() for item in args.episodes.split(",") if item.strip()]
        known = {episode.episode_id for episode in EPISODES}
        unknown = [item for item in episode_ids if item not in known]
        if unknown:
            raise BacktestError(f"unknown episodes {unknown}; available {sorted(known)}")
    report = backtest(episode_ids)
    document = report.as_dict()
    document["schema"] = RESULT_SCHEMA
    document["status"] = RESEARCH_ONLY
    out_dir = Path(args.out)
    written = {}
    if not args.json:
        written["json"] = _write(out_dir / "report.json",
                                 json.dumps(document, ensure_ascii=False, sort_keys=True,
                                            indent=2) + "\n")
        written["markdown"] = _write(out_dir / "report.md", report_markdown(report))
    return {"report": document, "written": written, "verdict": report.verdict}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the oil market module on 1990/2019/2022")
    parser.add_argument("--episodes", default=None,
                        help="comma-separated episode ids (default: all three)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of writing the files")
    parser.add_argument("--quiet", action="store_true", help="write files, print a one-line summary")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except BacktestError as error:
        print(f"backtest error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result["report"], ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    document = result["report"]
    metrics = document["metrics"]
    if args.quiet:
        print(f"{document['verdict']} | MAE {metrics['mae_model_usd']:.2f} vs naive "
              f"{metrics['mae_baseline_usd']:.2f} | {result['written']['markdown']['path']}")
        return 0
    print("Бэктест нефтяного модуля (research-only, не прогноз и не допуск)")
    print(f"  verdict: {document['verdict']}")
    print(f"  MAE модели: {metrics['mae_model_usd']:.2f} USD/bbl; "
          f"наивный «без шока»: {metrics['mae_baseline_usd']:.2f}; "
          f"skill: {metrics['skill_score_vs_no_shock']:+.3f}")
    print(f"  направление: {metrics['direction_hits']}/{metrics['episodes']}; "
          f"порядок (0.5–2x): {metrics['order_hits']}/{metrics['episodes']}")
    print("  эпизоды:")
    for item in document["episodes"]:
        print(f"    {item['episode_id']:<12} факт Δ {item['actual_delta_usd']:+.1f} "
              f"модель Δ {item['model_delta_usd']:+.2f}  ошибка {item['error_usd']:+.2f} "
              f"ratio {item['ratio_model_to_actual'] if item['ratio_model_to_actual'] is None else format(item['ratio_model_to_actual'], '.2f')}"
              f"  {'OK' if item['direction_ok'] else 'FAIL'}/"
              f"{'OK' if item['order_ok'] else 'FAIL'}")
    if document["where_worse"]:
        print(f"  хуже наивного: {', '.join(document['where_worse'])}")
    for name, info in result["written"].items():
        print(f"  {name}: {info['path']} ({info['bytes']} B, sha256 {info['sha256'][:16]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
