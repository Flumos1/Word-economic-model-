#!/usr/bin/env python3
"""One-command M1 acceptance: Client Pack v2 (with PDF) + Bridge + Simulator demo.

The bundle is an *aggregator of checks*, not a model:

1. builds the reduced-form forecast→world bridge into a temporary directory (``--partial`` allowed,
   documented as ``PARTIAL_INPUTS``);
2. builds the Client Pack v2 with that bridge, blocks and PDF export;
3. drives the six-step scripted Simulator demo in-process;
4. verifies every acceptance line of M1 (docs/207, docs/230) and prints a machine-readable summary.

Nothing is written into the repository: the bridge and the pack go to ``--out`` (a temporary
directory by default).  The tool never claims an admitted model, accuracy, ROI or a deployed service.

Usage::

    python tools/m1_demo_bundle.py --check      # verify the plan of checks, build nothing
    python tools/m1_demo_bundle.py --run        # full acceptance run
    python tools/m1_demo_bundle.py --run --no-pdf --json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import client_pack_v2 as pack  # noqa: E402
import simulator_demo as demo  # noqa: E402

SCHEMA = "amfe.m1.acceptance.v1"
STATUS = "RESEARCH_ONLY"

#: Every M1 acceptance line this bundle can prove locally (docs/207 §3, docs/230).
CHECKS: tuple[dict, ...] = (
    {"id": "bridge_built", "title": "Мост прогноз→мир собран (partial допускается)",
     "how": "tools/forecast_world_bridge.py --partial"},
    {"id": "client_pack", "title": "Client Pack v2 собран (9 модулей + Bridge/Blocks)",
     "how": "tools/client_pack_v2.py --include-blocks"},
    {"id": "client_pack_pdf", "title": "PDF-версии Client Pack (Executive/Methodology/Licence)",
     "how": "headless LibreOffice; fail-open без него"},
    {"id": "no_violations", "title": "Нет claims о допуске и сырых данных",
     "how": "pack.status == RESEARCH_ONLY, admission=false, checks.no_raw_data"},
    {"id": "simulator_demo", "title": "Simulator: 6 шагов, все RESEARCH_ONLY с digest",
     "how": "tools/simulator_demo.py --run"},
)


def build_bridge(out_dir: Path) -> dict:
    """Build the reduced-form bridge (partial mode) into ``out_dir``; return a small summary."""
    command = [sys.executable, str(ROOT / "tools" / "forecast_world_bridge.py"),
               "--feed", str(ROOT / "build" / "product" / "forecast_latest.json"),
               "--dataset", "figaro", "--partial", "--out", str(out_dir),
               "--generated-at", "2026-11-25T00:00:00Z"]
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    report = out_dir / "forecast_world.json"
    if not report.is_file():
        raise RuntimeError(f"bridge was not written: {result.stderr.strip()[:400]}")
    document = json.loads(report.read_text(encoding="utf-8"))
    return {
        "path": "forecast_world_bridge/forecast_world.json",
        "status": document.get("status"),
        "scenarios": sorted((document.get("scenarios") or {}).keys()) if isinstance(
            document.get("scenarios"), dict) else None,
        "admission": document.get("admission"),
        "checks": {"sfc_pass": (document.get("checks") or {}).get("sfc_pass"),
                   "no_raw_data": (document.get("checks") or {}).get("no_raw_data")},
        "exit_code": result.returncode,
    }


def run_bundle(out_dir: Path, *, make_pdf: bool = True, keep_bridge: bool = False) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The Client Pack refuses a bridge outside its artefact roots, so the bridge must live inside
    # build/.  It goes to a dedicated sub-directory (never the default search path) and is removed
    # afterwards unless --keep-bridge is given, so the repository stays clean.
    bridge_dir = ROOT / "build" / "m1_bundle" / "forecast_world_bridge"
    results: list[dict] = []

    def add(check_id: str, ok: bool, detail: dict) -> None:
        results.append({"id": check_id, "ok": bool(ok), "detail": detail})

    try:
        # 1. bridge
        bridge = build_bridge(bridge_dir)
        add("bridge_built", bridge["status"] in ("RESEARCH_ONLY", "PARTIAL_INPUTS"), bridge)

        # 2. client pack
        pack_dir = out_dir / "client_pack"
        summary = pack.build_pack(pack_dir, inputs_root=ROOT / "build",
                                  scenario_id="hormuz_closed_6m", client="M1 Demo",
                                  generated_at="2026-11-25T00:00:00Z", root=ROOT,
                                  bridge_path=str(bridge_dir / "forecast_world.json"),
                                  include_blocks=True, make_pdf=make_pdf)
        add("client_pack", summary.get("ok") is True, {
            "ok": summary.get("ok"), "status": summary.get("status"),
            "files": len(summary.get("files") or []), "pdfs": summary.get("pdfs") or [],
            "modules": summary["numbers"]["modules"],
            "bridge_available": summary["numbers"]["bridge"]["available"],
            "bridge_scenarios": summary["numbers"]["bridge"]["scenarios"],
        })
        add("client_pack_pdf", bool(summary.get("pdfs")), {"pdfs": summary.get("pdfs") or []})

        manifest = json.loads((pack_dir / "07_Reproducibility.json").read_text(encoding="utf-8"))
        machine = json.loads((pack_dir / "04_Machine_Readable.json").read_text(encoding="utf-8"))
        add("no_violations", machine.get("status") == STATUS
            and machine["admission"]["admitted"] is False, {
                "status": machine.get("status"),
                "admitted": machine["admission"]["admitted"],
                "deterministic_files": manifest.get("file_count"),
            })
    finally:
        if not keep_bridge:
            shutil.rmtree(ROOT / "build" / "m1_bundle", ignore_errors=True)

    # 3. simulator demo
    document = demo.runbook_document()
    app = demo.simulator_ui.SimulatorApp(geopolitics_mode="synthetic")
    sim = demo.run_demo(document, app)
    add("simulator_demo", sim["steps_run"] == 6 and sim["all_research_only"], {
        "steps_run": sim["steps_run"], "all_research_only": sim["all_research_only"],
        "scenarios": [step["scenario"] for step in sim["steps"]],
    })

    failed = [item["id"] for item in results if not item["ok"]]
    return {
        "schema": SCHEMA,
        "kind": "m1_acceptance_bundle",
        "status": STATUS,
        "admission": False,
        "out": str(out_dir).replace("\\", "/"),
        "ok": not failed,
        "failed": failed,
        "checks": results,
        "disclaimer": ("Aggregated M1 checks on declared assumptions; no model is admitted, no "
                       "forecast accuracy or ROI is claimed and no service is deployed."),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="list the acceptance checks and exit")
    parser.add_argument("--run", action="store_true", help="run the full acceptance bundle")
    parser.add_argument("--out", default=None, help="output directory (default: a temp directory)")
    parser.add_argument("--no-pdf", action="store_true", help="skip the PDF export step")
    parser.add_argument("--keep-bridge", action="store_true",
                        help="keep build/m1_bundle/forecast_world_bridge afterwards (default: remove)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    return parser


def _configure_output() -> None:
    """Force UTF-8 on stdout/stderr so Cyrillic and ``→`` survive a CP1251 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
            pass


def main(argv=None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    if args.check:
        print(json.dumps({"schema": SCHEMA, "status": STATUS, "checks": CHECKS},
                         ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if not args.run:
        print(json.dumps({"status": STATUS, "usage":
                          "python tools/m1_demo_bundle.py --check|--run"}, ensure_ascii=False))
        return 0
    owns_dir = args.out is None
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="amfe-m1-"))
    try:
        summary = run_bundle(out_dir, make_pdf=not args.no_pdf,
                             keep_bridge=args.keep_bridge)
    except Exception as error:  # fail-closed: never pretend the bundle passed
        print(json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    finally:
        if owns_dir and not args.json:
            pass  # keep the files for inspection; the path is printed below
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
