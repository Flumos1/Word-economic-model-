"""MRIO data-track CLI: source check, local validation and the aggregate-only manifest (docs/121).

Commands
--------
    python tools/mrio_data.py --check                 # source matrix + live probe + local status
    python tools/mrio_data.py --check --no-network    # offline (recorded facts only)
    python tools/mrio_data.py --validate              # validate the local dataset(s) under the root
    python tools/mrio_data.py --manifest --out generated/mrio_manifest.json

Exit codes
----------
``0`` -- data available and valid (``--check`` / ``--manifest``) or validation passed;
``1`` -- validation failed (schema / accounting identities / P2 scale);
``2`` -- ``BLOCKED_DATA``: no local MRIO table (the manifest is still written, with the reason).

The tool never downloads third-party observations and never writes raw values: only hashes,
counts and validation aggregates reach the manifest.  A network probe reads at most 2 KiB per URL
and records the HTTP status; a failure is reported, not invented.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.mrio import (  # noqa: E402
    DATASET_STATUSES,
    MIN_COUNTRIES,
    MIN_INDUSTRIES,
    MRIOError,
    STATUS_AVAILABLE,
    STATUS_BLOCKED,
    available_datasets,
    build_manifest,
    load_dataset,
    mrio_root,
    probe_sources,
    source_status_summary,
    validate_dataset,
    write_manifest,
)

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
DEFAULT_MANIFEST = ROOT / "generated" / "mrio_manifest.json"


def _print(message: str = "") -> None:
    print(message)


def _dataset_status(root, enforce_p2_scale: bool = True) -> tuple[str, str | None, dict | None]:
    """``(status, reason, describe)`` for the local data root; never raises."""
    try:
        ids = available_datasets(root)
    except MRIOError as exc:
        return STATUS_BLOCKED, str(exc), None
    if not ids:
        return STATUS_BLOCKED, ("нет '<dataset_id>/dataset.json' в корне данных; "
                                "см. docs/121 §3-4"), None
    failures = []
    for dataset_id in ids:
        try:
            loaded = load_dataset(dataset_id, root, enforce_p2_scale=enforce_p2_scale)
        except MRIOError as exc:
            failures.append(f"{dataset_id}: {exc}")
            continue
        return STATUS_AVAILABLE, None, {
            "dataset_id": dataset_id,
            "path": str(loaded.path),
            "sha256": loaded.sha256,
            "bytes": loaded.size_bytes,
            "dataset": loaded.dataset.describe(),
            "validation": loaded.report.as_dict(),
        }
    return STATUS_BLOCKED, "; ".join(failures), None


def command_check(args) -> int:
    probes = probe_sources(network=not args.no_network, timeout=args.timeout)
    summary = source_status_summary(probes)
    status, reason, describe = _dataset_status(args.root, enforce_p2_scale=not args.no_p2_gate)
    document = {
        "action": "check",
        "status": status,
        "reason": reason,
        "p2_requirements": {"min_countries": MIN_COUNTRIES, "min_industries": MIN_INDUSTRIES,
                            "units": "USD_mn"},
        "sources": probes,
        "source_summary": summary,
        "dataset": describe,
    }
    if args.json:
        _print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print("MRIO data check (docs/121)")
        _print(f"  источников в матрице: {summary['sources']}  "
               f"по статусам: {summary['by_status']}")
        _print(f"  API без ключа: {', '.join(summary['with_data_api_no_key']) or '-'}")
        _print(f"  требуют ключ/лицензию: {', '.join(summary['blocked']) or '-'}")
        for probe in probes:
            detail = probe["probe"]
            if probe["network"]:
                _print(f"  [{'OK ' if detail.get('reachable') else 'FAIL'}] "
                       f"{probe['source_id']:<16} HTTP {detail.get('http_status')} "
                       f"{detail.get('elapsed_ms')} ms  {probe['status_recorded']}")
            else:
                _print(f"  [off] {probe['source_id']:<16} {probe['status_recorded']} "
                       f"(сеть отключена)")
        _print(f"  локальные данные: {status}" + (f" -- {reason}" if reason else ""))
        if describe:
            data = describe["dataset"]
            _print(f"    {describe['dataset_id']}: {data['countries']} стран x "
                   f"{data['industries']} отраслей, годы {data['years']}, {data['units']}")
            _print(f"    sha256 {describe['sha256']}  digest {data['digest']}")
            _print(f"    валидация: ok={describe['validation']['ok']}  "
                   f"checks={describe['validation']['checks']}")
    return EXIT_OK if status == STATUS_AVAILABLE else EXIT_BLOCKED


def command_validate(args) -> int:
    dataset_id = args.dataset or None
    ids = [dataset_id] if dataset_id else None
    if ids is None:
        try:
            ids = available_datasets(args.root)
        except MRIOError as exc:
            _print(f"{STATUS_BLOCKED}: {exc}")
            return EXIT_BLOCKED
    if not ids:
        _print(f"{STATUS_BLOCKED}: нет наборов для валидации; см. docs/121 §3-4")
        return EXIT_BLOCKED
    failed = 0
    for current in ids:
        try:
            loaded = load_dataset(current, args.root, enforce_p2_scale=not args.no_p2_gate)
        except MRIOError as exc:
            _print(f"[FAIL] {current}: {exc}")
            failed += 1
            continue
        report = loaded.report
        status = "OK  " if report.ok else "FAIL"
        _print(f"[{status}] {current}: {report.metrics['countries']} стран x "
               f"{report.metrics['industries']} отраслей, {report.metrics['years']}, "
               f"max|row|={report.metrics['max_abs_row_residual']:.3e}, "
               f"max|col|={report.metrics['max_abs_column_residual']:.3e}, "
               f"sum(va)-sum(y)={report.metrics['global_difference_va_minus_y']:.3e}")
        if not report.ok:
            failed += 1
            for error in report.errors[:10]:
                _print(f"       - {error}")
        if args.json:
            _print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK if failed == 0 else EXIT_INVALID


def command_manifest(args) -> int:
    manifest = build_manifest(args.root, enforce_p2_scale=not args.no_p2_gate)
    out = Path(args.out)
    digest = write_manifest(manifest, out)
    status = manifest["status"]
    if args.json:
        _print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print(f"manifest: {out}")
        _print(f"  status: {status}" + (f"  reason: {manifest['reason']}"
                                        if manifest.get("reason") else ""))
        _print(f"  sha256: {digest}")
        for entry in manifest["datasets"]:
            line = f"  {entry['dataset_id']}: {entry['status']}"
            if entry.get("sha256"):
                line += (f"  {entry.get('countries')}x{entry.get('industries')}"
                         f"  sha256 {entry['sha256']}")
            if entry.get("reason"):
                line += f"  ({entry['reason'][:120]})"
            _print(line)
        _print(f"  sources: {len(manifest.get('sources') or [])} (см. docs/121 §1)")
    if status not in DATASET_STATUSES:
        return EXIT_INVALID
    return EXIT_OK if status == STATUS_AVAILABLE else EXIT_BLOCKED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MRIO data track: source check, validation and manifest (docs/121)")
    parser.add_argument("--root", default=None,
                        help="локальный корень данных MRIO (по умолчанию $AMFE_MRIO_ROOT)")
    parser.add_argument("--dataset", default=None,
                        help="id набора для --validate (по умолчанию все наборы в корне)")
    parser.add_argument("--out", default=str(DEFAULT_MANIFEST),
                        help="путь манифеста для --manifest (по умолчанию generated/mrio_manifest.json)")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="таймаут одного сетевого зонда, с (по умолчанию 8)")
    parser.add_argument("--no-network", action="store_true",
                        help="не обращаться в сеть: только записанные факты")
    parser.add_argument("--no-p2-gate", action="store_true",
                        help="не требовать 10+ стран x 30+ отраслей (для маленьких фикстур)")
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true",
                         help="матрица источников + сетевой зонд + статус локальных данных")
    actions.add_argument("--validate", action="store_true", help="валидировать локальные наборы")
    actions.add_argument("--manifest", action="store_true", help="записать манифест с хешами")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.validate:
        return command_validate(args)
    if args.manifest:
        return command_manifest(args)
    return command_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
