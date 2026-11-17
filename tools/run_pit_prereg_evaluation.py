#!/usr/bin/env python3
"""G4-E: evaluation wrapper pinned to the PIT preregistration 0.3.

The wrapper does not forecast by itself: it takes fresh ``run_real_evaluation.py`` v5 reports
(``--extended-c4``), verifies every pin the preregistration declares (preregistration file SHA,
protocol v5, protocol v4, reconstruction dossier, coverage-audit tree digest) and assembles the
preregistration-shaped report:

* C4 on W1_EXT 1990-01..2006-12 with the REAL_TIME/reconstructed origin split;
* the mandatory sensitivity slices: REAL_TIME-only, reconstructed-origin-excluded and tape-free
  are the same W1 window (origins >= the first real vintage 1996-12), leave-one-episode-out comes
  from the report's C4 episode table;
* C1/C2/C5 only from the REAL_TIME windows;
* a pass that relies on reconstructed origins is labelled CONDITIONAL_DATA_CLASS and is never an
  unconditional PASS; no admission is claimed (``admission=false``, status PROVISIONAL).

Fail-closed: any pin mismatch or malformed/foreign report stops the run with exit code 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "amfe.pit_prereg_evaluation.v1"
STATUS = "PROVISIONAL"
ADMISSION = False


class PreregError(RuntimeError):
    """A pin or a report failed validation."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk.replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def read_json(path: Path, label: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreregError(f"{label} is unreadable: {path}: {exc}") from None


def check_pins(prereg: dict, prereg_path: Path, root: Path = ROOT) -> dict:
    """Verify every pin declared by the preregistration; raise PreregError on a mismatch."""
    basis = prereg.get("protocol_basis", {})
    datas = prereg.get("data_basis", {})
    coverage = datas.get("coverage_audit", {})
    checks: dict[str, dict] = {}

    def check(name: str, path: Path, expected: str) -> None:
        if not Path(path).exists():
            raise PreregError(f"{name} file is missing: {path}")
        actual = sha256_file(path)
        checks[name] = {"file": str(Path(path).relative_to(root)), "sha256": actual,
                        "match": actual == expected}
        if actual != expected:
            raise PreregError(f"{name} sha256 mismatch: expected {expected}, got {actual}")

    if "v5" not in basis or "sha256" not in basis["v5"]:
        raise PreregError("preregistration has no protocol v5 pin")
    check("protocol_v5", root / basis["v5"]["file"], basis["v5"]["sha256"])
    if "v4" in basis and basis["v4"].get("sha256"):
        check("protocol_v4", root / basis["v4"]["file"], basis["v4"]["sha256"])
    dossier = datas.get("reconstruction_dossier", {})
    if dossier.get("sha256"):
        check("reconstruction_dossier", root / dossier["file"], dossier["sha256"])
    coverage_file = coverage.get("file")
    if coverage_file:
        # the preregistration annotates the git-ignored path: "path (git-ignored; ...)"
        coverage_file = coverage_file.split(" (", 1)[0].strip()
    expected_tree = coverage.get("tree_digest")
    if coverage_file and expected_tree:
        path = root / coverage_file
        if not path.exists():
            raise PreregError(f"coverage audit is missing: {path}")
        audit = read_json(path, "coverage audit")
        actual = ((audit.get("artifacts") or {}).get("tree_digest")
                  or (audit.get("data_root") or {}).get("tree_digest")
                  or audit.get("tree_digest"))
        checks["coverage_audit"] = {"file": str(path.relative_to(root)),
                                    "tree_digest": actual, "match": actual == expected_tree}
        if actual != expected_tree:
            raise PreregError(f"coverage tree_digest mismatch: expected {expected_tree}, got {actual}")
    pin_file = Path(str(prereg_path) + ".sha256.txt")
    if pin_file.exists():
        expected_file = pin_file.read_text(encoding="utf-8").split()[0]
        actual_file = sha256_file(Path(prereg_path))
        checks["preregistration_file"] = {"file": Path(prereg_path).name, "sha256": actual_file,
                                          "match": actual_file == expected_file}
        if actual_file != expected_file:
            raise PreregError("preregistration file sha256 does not match its pin")
    return checks


def _pass(criteria: dict, key: str) -> bool | None:
    entry = criteria.get(key) or {}
    return entry.get("pass")


def summarise_report(report: dict, prereg: dict, standard: dict | None = None) -> dict:
    """Extract the preregistration-relevant fields from one v5 evaluation report."""
    protocol_sha = (report.get("protocol") or {}).get("sha256")
    expected = prereg["protocol_basis"]["v5"]["sha256"]
    if protocol_sha != expected:
        raise PreregError(f"report protocol sha256 {protocol_sha} does not match the v5 pin {expected}")
    reconstruction = report.get("reconstruction") or {}
    windows = report.get("windows") or {}
    w1_ext = windows.get("W1_EXT") or {}
    w1 = windows.get("W1") or {}
    w2 = windows.get("W2") or {}
    admission = report.get("admission") or {}
    criteria = admission.get("criteria") or {}
    c4 = criteria.get("C4") or {}
    # In an extended-C4 run the extended W1_EXT origins replace the standard W1 C4 window
    # (``c4.extended`` says so); the standard REAL_TIME-only C4 comes from a separate standard run.
    c4_window = (c4.get("per_window") or {}).get("W1") or {}
    c4_extended = (c4.get("extended") or {}).get("W1") or {}
    episodes = ((report.get("c4_episodes") or {}).get("W1_EXT") or {})
    loo = {name: {"origins": (value.get("RMSE") or {}).get("origins"),
                  "relative_loss_leave_one_out": (value.get("RMSE") or {}).get("relative_loss_leave_one_out")}
           for name, value in episodes.items()}
    counts = {
        "w1_ext_origins_total": reconstruction.get("w1_origins_total", w1_ext.get("origins")),
        "w1_ext_origins_real_time": reconstruction.get("w1_origins_real_time"),
        "w1_ext_origins_reconstructed": reconstruction.get("w1_origins_reconstructed"),
        "first_real_vintage": reconstruction.get("first_real_vintage"),
        "w1_origins": w1.get("used_origins") and len(w1["used_origins"]),
        "w2_origins": w2.get("used_origins") and len(w2["used_origins"]),
        "w1_data_class": w1.get("data_class"),
        "w2_data_class": w2.get("data_class"),
        "w1_ext_data_class": w1_ext.get("data_class"),
    }
    reconstructed_share = None
    if counts["w1_ext_origins_total"]:
        reconstructed_share = round((counts["w1_ext_origins_reconstructed"] or 0)
                                    / counts["w1_ext_origins_total"], 6)
    c4_status = (c4_window.get("RMSE") or {}).get("status") or c4_window.get("status")
    conditional = bool(c4_status == "PASS"
                       and c4_extended.get("data_class") == "REAL_TIME_RECONSTRUCTED")
    overall = {
        "C1": _pass(criteria, "C1"),
        "C2": _pass(criteria, "C2"),
        "C5": _pass(criteria, "C5"),
        "C4_W1_EXT": c4_status,
        "conditional_data_class": conditional,
    }
    return {
        "report_file": None,
        "candidate": report.get("candidate"),
        "target": report.get("target"),
        "horizon": report.get("horizon"),
        "data_class": report.get("data_class"),
        "conditional_data_class": report.get("conditional_data_class"),
        "admission_status": admission.get("status"),
        "windows": counts,
        "reconstructed_origin_share": reconstructed_share,
        "criteria": overall,
        "c4_w1_ext_relative_loss": (c4_window.get("RMSE") or {}).get("relative_loss_recession"),
        "c4_w1_ext_recession_origins": (c4_window.get("RMSE") or {}).get("recession_origins"),
        "c4_extended": {"data_class": c4_extended.get("data_class"),
                        "origins": c4_extended.get("n_origins"),
                        "source": c4_extended.get("source")},
        "c4_leave_one_episode_out": loo,
        "slices": _slices(standard, loo),
    }


def _slices(standard: dict | None, loo: dict) -> dict:
    """Mandatory preregistration slices; REAL_TIME-only needs a standard (non-extended) v5 run."""
    if standard is None:
        not_computed = {"window": "W1", "c4_status": "NOT_COMPUTED_IN_EXTENDED_RUN",
                        "note": "run tools/run_real_evaluation.py without --extended-c4 and pass it via "
                                "--standard-report to obtain the REAL_TIME-only C4 slice"}
        return {"real_time_only": dict(not_computed),
                "reconstructed_excluded": dict(not_computed),
                "tape_free": dict(not_computed),
                "leave_one_episode_out": loo}
    criteria = ((standard.get("admission") or {}).get("criteria") or {}).get("C4") or {}
    std_window = (criteria.get("per_window") or {}).get("W1") or {}
    status = (std_window.get("RMSE") or {}).get("status") or std_window.get("status")
    data_class = ((standard.get("windows") or {}).get("W1") or {}).get("data_class")
    slice_entry = {"window": "W1", "data_class": data_class, "c4_status": status,
                   "relative_loss": (std_window.get("RMSE") or {}).get("relative_loss_recession"),
                   "recession_origins": (std_window.get("RMSE") or {}).get("recession_origins")}
    return {"real_time_only": dict(slice_entry),
            "reconstructed_excluded": dict(slice_entry, note="W1 starts at the first real vintage"),
            "tape_free": dict(slice_entry),
            "leave_one_episode_out": loo}


def build_report(prereg: dict, prereg_path: Path, report_paths: list[Path], root: Path = ROOT,
                 standard_paths: list[Path] | None = None) -> dict:
    pins = check_pins(prereg, prereg_path, root)
    standards: dict[str, dict] = {}
    for path in standard_paths or []:
        data = read_json(path, "standard evaluation report")
        standards[data.get("candidate")] = data
    candidates = []
    for path in report_paths:
        report = read_json(path, "evaluation report")
        summary = summarise_report(report, prereg, standards.get(report.get("candidate")))
        summary["report_file"] = str(Path(path).relative_to(root)) if Path(path).is_relative_to(root) else str(path)
        summary["report_sha256"] = sha256_file(path)
        candidates.append(summary)
    labels = []
    for item in candidates:
        criteria = item["criteria"]
        hard = [key for key in ("C1", "C2", "C5") if criteria[key] is False]
        c4 = criteria["C4_W1_EXT"]
        if hard or c4 in ("FAIL", "INCONCLUSIVE", "NOT_APPLICABLE", None):
            label = "FAIL" if hard else "CANDIDATE"
        elif criteria["conditional_data_class"]:
            label = "CONDITIONAL_DATA_CLASS"
        else:
            label = "PASS"
        labels.append({"candidate": item["candidate"], "label": label,
                       "conditional_data_class": bool(criteria["conditional_data_class"]),
                       "gating_failures": hard})
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "admission": ADMISSION,
        "preregistration": {
            "file": str(Path(prereg_path).name),
            "preregistration_id": prereg.get("preregistration_id"),
            "version": prereg.get("version"),
            "status": prereg.get("status"),
            "sha256": sha256_file(Path(prereg_path)),
            "content_sha256": prereg.get("content_sha256"),
        },
        "pins": pins,
        "pins_ok": all(entry.get("match") for entry in pins.values()),
        "candidates": candidates,
        "labels": labels,
        "limitations": [
            "PROVISIONAL: the preregistration is PROPOSED; this is not an admission and not a final result",
            "C4 on W1_EXT relies on reconstructed origins: a pass is CONDITIONAL_DATA_CLASS, never unconditional",
            "REAL_TIME-only, reconstructed-excluded and tape-free slices are the REAL_TIME W1 window",
            "no raw observations, forecasts or third-party values are stored: aggregates and digests only",
            "T2/T3 are outside this evaluation; no model parameter was tuned",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="evaluation pinned to the PIT preregistration 0.3")
    parser.add_argument("--prereg", default=str(ROOT / "protocols" / "pit_preregistration_0_3.json"))
    parser.add_argument("--report", action="append", default=None,
                        help="v5 extended-C4 evaluation report (repeatable)")
    parser.add_argument("--out", default=str(ROOT / "protocols" / "pit_prereg_evaluation_report.json"))
    parser.add_argument("--check", action="store_true", help="validate pins and reports only, write nothing")
    parser.add_argument("--standard-report", action="append", default=None,
                        help="standard (non-extended) v5 report for the REAL_TIME-only C4 slice")
    parser.add_argument("--root", default=str(ROOT), help="repository root for pin resolution")
    parser.add_argument("--json", action="store_true", help="print the report to stdout")
    args = parser.parse_args(argv)
    prereg_path = Path(args.prereg)
    prereg = read_json(prereg_path, "preregistration")
    reports = [Path(p) for p in (args.report or [])]
    try:
        if not reports:
            raise PreregError("at least one --report is required")
        report = build_report(prereg, prereg_path, reports, root=Path(args.root),
                             standard_paths=[Path(p) for p in (args.standard_report or [])])
    except PreregError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps({"ok": True, "pins_ok": report["pins_ok"],
                          "candidates": report["labels"]}, ensure_ascii=False))
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                   newline="\n")
    print(json.dumps({"status": "OK", "out": str(out), "pins_ok": report["pins_ok"],
                      "labels": report["labels"]}, ensure_ascii=False))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
