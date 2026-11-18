#!/usr/bin/env python3
"""Country forecast CLI (task-60 / UC-06/07/08), RESEARCH_ONLY.

Examples:
  python tools/country_forecast.py --frequency quarterly --countries EA,CN,GB \
      --targets GDP,CPI --horizons 1,2,3,4
  python tools/country_forecast.py --frequency annual --countries EA,CN,GB \
      --targets GDP,CPI --horizons 1,2,3,4
  python tools/country_forecast.py --frequency quarterly --countries CN --require-real

The command writes ``report.json`` and ``report.md`` (aggregates only) under
``build/country_forecast`` by default.  Files are never written under ``data/``,
``protocols/`` or ``src/``.  Missing real data is not imputed: the report labels a
synthetic fixture, and ``--require-real`` turns any such gap into a non-zero exit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.forecast import country as cf  # noqa: E402

DEFAULT_OUT = ROOT / "build" / "country_forecast"
PROTECTED_DIRS = (ROOT / "data", ROOT / "protocols", ROOT / "src")


def _split(value: str) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _protected(path: Path) -> bool:
    resolved = path.resolve()
    for protected in PROTECTED_DIRS:
        try:
            resolved.relative_to(protected.resolve())
            return True
        except ValueError:
            continue
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Country nowcast/forecast for EA/CN/GB (RESEARCH_ONLY; aggregates only)"
    )
    parser.add_argument("--countries", default="EA,CN,GB",
                        help="comma-separated ISO-like ids/aliases: EA, CN/CHN, GB/GBR/UK")
    parser.add_argument("--targets", default="GDP,CPI", help="comma-separated targets: GDP, CPI")
    parser.add_argument("--horizons", default="1,2,3,4", help="comma-separated horizons 1..4")
    parser.add_argument("--frequency", default="quarterly", choices=("quarterly", "annual", "Q", "A"),
                        help="quarterly (default) or annual; annual uses the WDI cache when present")
    parser.add_argument("--origin", default=None,
                        help="as-of date (ISO); default: latest release date in the loaded series")
    parser.add_argument("--data-root", default=None,
                        help="root of the optional local PIT CSV provider (default data/external)")
    parser.add_argument("--cache-root", default=None,
                        help="root of the public quarterly cache (default data/raw/country); "
                             "populate it with tools/fetch_country_data.py")
    parser.add_argument("--wb-root", default=None,
                        help="root of the World Bank WDI cache (default data/mrio/wb)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--generated-at", default=None, help="fixed timestamp for reproducible digests")
    parser.add_argument("--models", default=",".join(cf.MODEL_IDS),
                        help="comma-separated models: RW, AR_BIC, PANEL_AR1, BRIDGE")
    parser.add_argument("--require-real", action="store_true",
                        help="fail closed if any selected country/target has no real series")
    parser.add_argument("--json", action="store_true", help="print the full JSON report to stdout")
    parser.add_argument("--no-write", action="store_true", help="do not write report.json/report.md")
    return parser


def _fail(message: str) -> int:
    print(json.dumps({"status": "FAILED", "error": str(message)}, ensure_ascii=False), file=sys.stderr)
    return 2


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        countries = tuple(cf.normalize_country(item) for item in _split(args.countries))
        targets = tuple(cf.normalize_target(item) for item in _split(args.targets))
        horizons = tuple(int(item) for item in _split(args.horizons))
        models = tuple(item.strip().upper() for item in _split(args.models))
        frequency = cf.normalize_frequency(args.frequency)
        out = Path(args.out)
    except (ValueError, cf.CountryForecastError) as exc:
        return _fail(exc)
    if _protected(out):
        return _fail("refusing to write reports under data/, protocols/ or src/; use build/ or a temp dir")
    if out.exists() and not out.is_dir():
        return _fail(f"output path is not a directory: {out}")
    try:
        report = cf.build_report(
            countries=countries, targets=targets, horizons=horizons, freq=frequency,
            origin=args.origin, external_root=args.data_root, cache_root=args.cache_root,
            wb_root=args.wb_root,
            allow_synthetic=not args.require_real, generated_at=args.generated_at, models=models,
        )
    except (ValueError, cf.CountryForecastError) as exc:
        return _fail(exc)

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    markdown = cf.render_markdown(report)
    if not args.no_write:
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_bytes(text.encode("utf-8"))
        (out / "report.md").write_bytes(markdown.encode("utf-8"))
    if args.json:
        sys.stdout.write(text)
        return 0
    summary = {
        "status": report["status"],
        "frequency": report["frequency_label"],
        "origin": report["origin"],
        "countries": {entry["country"]: entry["status"] for entry in report["countries"]},
        "real_country_target_pairs": report["summary"]["real_country_target_pairs"],
        "synthetic_country_target_pairs": report["summary"]["synthetic_country_target_pairs"],
        "forecasts": report["summary"]["forecasts"],
        "oos_ok": report["summary"]["oos_ok"],
        "content_sha256": report["content_sha256"],
        "out": None if args.no_write else str(out),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
