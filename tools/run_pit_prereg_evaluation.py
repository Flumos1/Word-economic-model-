#!/usr/bin/env python3
"""G4-E: evaluation wrapper pinned to the PIT preregistration 0.3 (V2 re-release).

The wrapper does not forecast by itself: it takes fresh ``run_real_evaluation.py`` v5 reports
(``--extended-c4``), verifies every pin the preregistration declares (preregistration file SHA under
both pin names, the canonical ``content_sha256``, protocol v5, protocol v4, reconstruction dossier and
the coverage-audit tree digest recomputed from the data root) and assembles the
preregistration-shaped report:

* C4 on W1_EXT 1990-01..2006-12 with the REAL_TIME/reconstructed origin split, plus C4 on W2;
* the mandatory sensitivity slices as three **separate computations** (docs/172 §4):

  - ``real_time_only`` -- a standard (non-extended) v5 run on ``data/raw`` (``--standard-report``);
  - ``tape_free`` -- a standard (non-extended) v5 run on the reconstructed root, whose origins are
    floored at the first real vintage (``--tape-free-report``);
  - ``reconstructed_excluded`` -- C4 recomputed *inside* the primary extended run on its REAL_TIME
    origins only (``c4_slices.real_time_window`` of the primary report);

* leave-one-episode-out for 1990-91, 2001 (W1_EXT) and 2007-09 (W2);
* C1/C2/C5 only from the REAL_TIME windows, ``w1_data_class`` = REAL_TIME;
* a pass that relies on reconstructed origins is labelled CONDITIONAL_DATA_CLASS and is never an
  unconditional PASS; the run-level data class is reported separately from the conditional pass; no
  admission is claimed (``admission=false``, status PROVISIONAL).

Fail-closed: any pin mismatch, a missing pin, a malformed/foreign report, a standard report that
contains pre-1996 (reconstructed) origins (the cache-replay defect of docs/172) or two slices that
resolve to the same report stops the run with exit code 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "amfe.pit_prereg_evaluation.v2"
STATUS = "PROVISIONAL"
ADMISSION = False
FIRST_REAL_VINTAGE = "1996-12-31"
#: Mandatory preregistration sensitivity slices and the computation each one must come from.
SLICE_NAMES = ("real_time_only", "reconstructed_excluded", "tape_free")
STANDARD_SLICES = {"real_time_only": "data/raw", "tape_free": "reconstructed root"}
#: Both pin spellings used in the repository: <stem>.sha256.txt and <file>.json.sha256.txt.
PIN_SUFFIXES = (".sha256.txt", ".json.sha256.txt")


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


def canonical_content_sha256(document: dict) -> str:
    """SHA-256 of the canonical JSON without the ``content_sha256`` field itself (docs/166 §5)."""
    payload = {key: value for key, value in document.items() if key != "content_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pin_paths(prereg_path: Path) -> list[Path]:
    """Every accepted pin file name for the preregistration file, in lookup order."""
    path = Path(prereg_path)
    stem = path.with_suffix("") if path.suffix else path
    candidates = [Path(str(stem) + ".sha256.txt"), Path(str(path) + ".sha256.txt")]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def coverage_tree_digest(coverage_path: Path) -> str:
    """Recompute the data-root tree digest instead of trusting the stored field (docs/172 §2)."""
    import reconstruct_pit  # offline tool (stdlib + numpy); same definition as ``--g4``

    coverage_path = Path(coverage_path)
    return reconstruct_pit.tree_digest(coverage_path.parent,
                                      exclude=(coverage_path.name,))


def _stored_tree_digest(audit: dict) -> str | None:
    return ((audit.get("artifacts") or {}).get("tree_digest")
            or (audit.get("data_root") or {}).get("tree_digest")
            or audit.get("tree_digest"))


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
        checks[name] = {"file": _relative(path, root), "sha256": actual,
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
        stored = _stored_tree_digest(audit)
        recomputed = coverage_tree_digest(path)
        checks["coverage_audit"] = {
            "file": _relative(path, root),
            "tree_digest_pin": expected_tree,
            "tree_digest_recomputed": recomputed,
            "tree_digest_stored_in_audit": stored,
            "recomputed_from_data_root": True,
            "match": recomputed == expected_tree}
        if recomputed != expected_tree:
            raise PreregError(f"coverage tree_digest mismatch (recomputed from "
                              f"{path.parent}): expected {expected_tree}, got {recomputed}")
    # the preregistration's own SHA: both pin spellings are supported, at least one must exist
    pins = [pin for pin in pin_paths(prereg_path) if pin.exists()]
    if not pins:
        raise PreregError("preregistration pin file is missing: neither "
                          f"{pin_paths(prereg_path)[0].name} nor "
                          f"{pin_paths(prereg_path)[1].name} exists")
    actual_file = sha256_file(Path(prereg_path))
    for pin in pins:
        expected_file = pin.read_text(encoding="utf-8").split()[0]
        if actual_file != expected_file:
            raise PreregError(f"preregistration file sha256 does not match its pin {pin.name}: "
                              f"expected {expected_file}, got {actual_file}")
    checks["preregistration_file"] = {
        "file": Path(prereg_path).name, "sha256": actual_file,
        "pin_file": pins[0].name,
        "pin_files_checked": [pin.name for pin in pins],
        "match": True}
    # the canonical content hash must be recomputed, not copied
    declared_content = prereg.get("content_sha256")
    if not declared_content:
        raise PreregError("preregistration has no content_sha256")
    recomputed_content = canonical_content_sha256(prereg)
    checks["preregistration_content"] = {"declared": declared_content,
                                         "recomputed": recomputed_content,
                                         "match": recomputed_content == declared_content}
    if recomputed_content != declared_content:
        raise PreregError(f"preregistration content_sha256 mismatch: declared {declared_content}, "
                          f"recomputed {recomputed_content}")
    return checks


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _pass(criteria: dict, key: str) -> bool | None:
    entry = criteria.get(key) or {}
    return entry.get("pass")


def _c4_entry(block: dict | None) -> dict:
    """Normalise one C4 window block (RMSE loss) to the fields the report publishes.

    The relative loss is re-derived from the per-origin differentials inside the fresh run (the
    runner computes it from the 16 recession-origin losses); the per-origin array itself is never
    stored, only this aggregate and the conditional-test summary.
    """
    block = block or {}
    rmse = block.get("RMSE") or {}
    return {"status": rmse.get("status") or block.get("status"),
            "relative_loss": rmse.get("relative_loss_recession"),
            "recession_origins": rmse.get("recession_origins"),
            "min_recession_origins": rmse.get("min_recession_origins"),
            "mean_differential_recession": rmse.get("mean_differential_recession"),
            "conditional_gw_p": rmse.get("conditional_gw_p"),
            "favours_comparator_in_recession": rmse.get("favours_comparator_in_recession"),
            "pass": rmse.get("pass")}


def _episode_table(episode_blocks: dict) -> dict:
    """Leave-one-episode-out (and the in-episode ratio) for every episode of every window."""
    out: dict[str, dict] = {}
    for window, episodes in (episode_blocks or {}).items():
        for name, value in (episodes or {}).items():
            rmse = (value or {}).get("RMSE") or {}
            out[name] = {"window": window,
                         "origins": rmse.get("origins"),
                         "relative_loss_episode": rmse.get("relative_loss_episode"),
                         "relative_loss_leave_one_out": rmse.get("relative_loss_leave_one_out")}
    return {name: out[name] for name in sorted(out)}


def _validated_origins(window: dict, label: str, first_real: str) -> list:
    origins = list(window.get("used_origins") or [])
    if not origins:
        raise PreregError(f"{label} has no used_origins")
    leaked = [origin for origin in origins if str(origin) < first_real]
    if leaked:
        raise PreregError(f"{label} contains {len(leaked)} origin(s) before the first real vintage "
                          f"{first_real} (cache replay / contaminated run, docs/172 §4): "
                          f"{leaked[:3]}")
    return origins


def _slice_from_standard(name: str, standard: dict, path: Path, prereg: dict,
                         root: Path) -> dict:
    """One standard (non-extended) v5 run -> one mandatory sensitivity slice."""
    protocol_sha = (standard.get("protocol") or {}).get("sha256")
    expected = prereg["protocol_basis"]["v5"]["sha256"]
    if protocol_sha != expected:
        raise PreregError(f"{name} standard report protocol sha256 {protocol_sha} does not match "
                          f"the v5 pin {expected}")
    reconstruction = standard.get("reconstruction") or {}
    first_real = reconstruction.get("first_real_vintage") or FIRST_REAL_VINTAGE
    w1 = (standard.get("windows") or {}).get("W1") or {}
    origins = _validated_origins(w1, f"{name} standard report W1", first_real)
    data_class = w1.get("data_class")
    if data_class != "REAL_TIME":
        raise PreregError(f"{name} standard report W1 data_class is {data_class!r}, expected "
                          "'REAL_TIME'")
    c4 = (((standard.get("admission") or {}).get("criteria") or {}).get("C4") or {})
    entry = _c4_entry((c4.get("per_window") or {}).get("W1"))
    return {"window": "W1",
            "computation": "standard_v5_run",
            "origin_floor": w1.get("origin_floor") or first_real,
            "origins": len(origins),
            "first_origin": w1.get("first_origin") or origins[0],
            "data_class": "REAL_TIME",
            "c4_status": entry["status"],
            "relative_loss": entry["relative_loss"],
            "recession_origins": entry["recession_origins"],
            "report_file": _relative(path, root),
            "report_sha256": sha256_file(path)}


def _slice_from_primary(report: dict, path: Path, prereg: dict, root: Path) -> dict:
    """``reconstructed_excluded``: C4 recomputed inside the primary extended run (REAL_TIME subset)."""
    block = ((report.get("c4_slices") or {}).get("real_time_window") or {})
    if not block:
        raise PreregError("the primary report has no c4_slices.real_time_window: rerun "
                          "tools/run_real_evaluation.py with --extended-c4 on a version that records "
                          "the in-run REAL_TIME C4 (docs/172 §4)")
    reconstruction = report.get("reconstruction") or {}
    first_real = reconstruction.get("first_real_vintage") or FIRST_REAL_VINTAGE
    windows_w1 = (report.get("windows") or {}).get("W1") or {}
    origins = _validated_origins(windows_w1, "primary report W1", first_real)
    if block.get("data_class") != "REAL_TIME":
        raise PreregError(f"primary c4_slices.real_time_window data_class is "
                          f"{block.get('data_class')!r}, expected 'REAL_TIME'")
    if int(block.get("origins") or 0) != len(origins):
        raise PreregError("primary c4_slices.real_time_window origins disagree with windows.W1")
    entry = _c4_entry(block.get("losses"))
    return {"window": "W1",
            "computation": "primary_run_real_time_subset",
            "origin_floor": block.get("origin_floor") or first_real,
            "origins": len(origins),
            "first_origin": block.get("first_origin") or windows_w1.get("first_origin"),
            "data_class": "REAL_TIME",
            "c4_status": entry["status"],
            "relative_loss": entry["relative_loss"],
            "recession_origins": entry["recession_origins"],
            "report_file": _relative(path, root),
            "report_sha256": sha256_file(path)}


def _slices(report: dict, report_path: Path, prereg: dict, standards: dict, root: Path) -> dict:
    """The three mandatory slices, each from its own computation; aliases fail closed."""
    missing = [name for name in STANDARD_SLICES if name not in standards]
    if missing:
        raise PreregError("mandatory sensitivity slice(s) not supplied: " + ", ".join(missing)
                          + " (--standard-report for real_time_only, --tape-free-report for tape_free)")
    entries = {}
    for name, (standard, path) in standards.items():
        entries[name] = _slice_from_standard(name, standard, path, prereg, root)
    entries["reconstructed_excluded"] = _slice_from_primary(report, report_path, prereg, root)
    seen: dict[str, str] = {}
    for name in SLICE_NAMES:
        entry = entries.get(name)
        if entry is None:
            raise PreregError(f"mandatory sensitivity slice {name} was not computed")
        signature = entry["report_sha256"]
        if signature in seen:
            raise PreregError(f"slices {seen[signature]} and {name} resolve to the same report "
                              f"({entry['report_file']}): the mandatory slices must be separate "
                              "computations, not aliases (docs/172 §4)")
        seen[signature] = name
    return {name: entries[name] for name in SLICE_NAMES}


def summarise_report(report: dict, prereg: dict, report_path: Path | None = None,
                     root: Path = ROOT, standards: dict | None = None) -> dict:
    """Extract the preregistration-relevant fields from one v5 evaluation report."""
    protocol_sha = (report.get("protocol") or {}).get("sha256")
    expected = prereg["protocol_basis"]["v5"]["sha256"]
    if protocol_sha != expected:
        raise PreregError(f"report protocol sha256 {protocol_sha} does not match the v5 pin {expected}")
    reconstruction = report.get("reconstruction") or {}
    first_real = reconstruction.get("first_real_vintage") or FIRST_REAL_VINTAGE
    windows = report.get("windows") or {}
    w1_ext = windows.get("W1_EXT") or {}
    w1 = windows.get("W1") or {}
    w2 = windows.get("W2") or {}
    if not w1_ext:
        raise PreregError("the primary report has no windows.W1_EXT: run tools/run_real_evaluation.py "
                          "with --extended-c4 (protocol v5)")
    w1_origins = _validated_origins(w1, "primary report W1", first_real)
    admission = report.get("admission") or {}
    criteria = admission.get("criteria") or {}
    c4 = criteria.get("C4") or {}
    # In an extended-C4 run the extended W1_EXT origins replace the standard W1 C4 window
    # (``c4.extended`` says so); the standard REAL_TIME-only C4 is a separate computation.
    c4_w1_ext = _c4_entry((c4.get("per_window") or {}).get("W1"))
    c4_w2 = _c4_entry((c4.get("per_window") or {}).get("W2"))
    loo = _episode_table(report.get("c4_episodes") or {})
    counts = {
        "w1_ext_origins_total": reconstruction.get("w1_origins_total", w1_ext.get("origins")),
        "w1_ext_origins_real_time": reconstruction.get("w1_origins_real_time"),
        "w1_ext_origins_reconstructed": reconstruction.get("w1_origins_reconstructed"),
        "first_real_vintage": first_real,
        "w1_origins": len(w1_origins),
        "w2_origins": len(w2.get("used_origins") or []),
        # the REAL_TIME-only window is REAL_TIME by construction: pre-1996 origins are rejected above
        "w1_data_class": "REAL_TIME",
        "w1_data_class_reported_by_run": w1.get("data_class"),
        "w1_data_class_relabelled": bool(w1.get("data_class")
                                         and w1.get("data_class") != "REAL_TIME"),
        "w2_data_class": w2.get("data_class"),
        "w1_ext_data_class": w1_ext.get("data_class"),
    }
    total = counts["w1_ext_origins_total"]
    reconstructed = counts["w1_ext_origins_reconstructed"] or 0
    reconstructed_share = round(reconstructed / total, 6) if total else None
    conditional_pass = bool(c4_w1_ext.get("status") == "PASS" and reconstructed > 0)
    overall = {
        "C1": _pass(criteria, "C1"),
        "C2": _pass(criteria, "C2"),
        "C5": _pass(criteria, "C5"),
        "C4_W1_EXT": c4_w1_ext.get("status"),
        "C4_W2": c4_w2.get("status"),
        # the conditional PASS flag: a C4 pass that rests on reconstructed origins
        "conditional_data_class": conditional_pass,
    }
    relies_on_reconstructed = bool(reconstructed > 0
                                   or (report.get("data_class") or "").startswith(
                                       "REAL_TIME_RECONSTRUCTED"))
    summary = {
        "report_file": None if report_path is None else _relative(report_path, root),
        "report_sha256": None if report_path is None else sha256_file(report_path),
        "candidate": report.get("candidate"),
        "target": report.get("target"),
        "horizon": report.get("horizon"),
        "data_class": report.get("data_class"),
        # the run-level class flag (C4 on W1_EXT relies on reconstructed origins) is NOT the pass flag
        "relies_on_reconstructed_origins": relies_on_reconstructed,
        "admission_status": admission.get("status"),
        "windows": counts,
        "reconstructed_origin_share": reconstructed_share,
        "criteria": overall,
        "c4_w1_ext": c4_w1_ext,
        "c4_w2": c4_w2,
        "c4_extended": {"data_class": ((c4.get("extended") or {}).get("W1") or {}).get("data_class"),
                        "origins": ((c4.get("extended") or {}).get("W1") or {}).get("n_origins"),
                        "source": ((c4.get("extended") or {}).get("W1") or {}).get("source")},
        "c4_leave_one_episode_out": loo,
    }
    if standards is not None and report_path is not None:
        summary["slices"] = _slices(report, report_path, prereg, standards, root)
    return summary


def build_report(prereg: dict, prereg_path: Path, report_paths: list[Path], root: Path = ROOT,
                 standard_reports: list[dict[str, Path]] | None = None) -> dict:
    """Assemble the preregistration report from fresh v5 runs.

    ``standard_reports`` has one entry per primary report (same order); each entry maps the mandatory
    slice name to a standard (non-extended) v5 report: ``real_time_only`` (run on ``data/raw``) and
    ``tape_free`` (run on the reconstructed root, origins floored at the first real vintage).
    ``reconstructed_excluded`` is recomputed from the primary extended report itself.
    """
    pins = check_pins(prereg, prereg_path, root)
    report_paths = [Path(path) for path in report_paths]
    pairings = list(standard_reports or [])
    if len(pairings) != len(report_paths):
        raise PreregError(f"every primary report needs its own standard reports: {len(report_paths)} "
                          f"primary report(s), {len(pairings)} pairing(s)")
    standards_seen: dict[str, dict] = {}
    candidates = []
    for path, pairing in zip(report_paths, pairings):
        report = read_json(path, "evaluation report")
        candidate_id = report.get("candidate")
        missing = [name for name in STANDARD_SLICES if name not in (pairing or {})]
        if missing:
            raise PreregError("mandatory sensitivity slice(s) not supplied: " + ", ".join(missing)
                              + " (--standard-report for real_time_only, --tape-free-report for tape_free)")
        standards = {}
        for name in STANDARD_SLICES:
            standard_path = Path(pairing[name])
            standard = read_json(standard_path, f"{name} standard evaluation report")
            if standard.get("candidate") != candidate_id:
                raise PreregError(f"{name} standard report candidate "
                                  f"{standard.get('candidate')!r} does not match the primary report "
                                  f"candidate {candidate_id!r}")
            standards[name] = (standard, standard_path)
            standards_seen[f"{candidate_id}:{name}"] = {
                "file": _relative(standard_path, root), "sha256": sha256_file(standard_path)}
        summary = summarise_report(report, prereg, path, root, standards)
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
                       "relies_on_reconstructed_origins": item["relies_on_reconstructed_origins"],
                       "c4_w1_ext": criteria["C4_W1_EXT"], "c4_w2": criteria["C4_W2"],
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
            "content_sha256_verified": bool(
                (pins.get("preregistration_content") or {}).get("match")),
        },
        "pins": pins,
        "pins_ok": all(entry.get("match") for entry in pins.values()),
        "standard_reports": {name: standards_seen[name] for name in sorted(standards_seen)},
        "slices_complete": True,
        "candidates": candidates,
        "labels": labels,
        "limitations": [
            "PROVISIONAL: the preregistration is PROPOSED; this is not an admission and not a final result",
            "C4 on W1_EXT relies on reconstructed origins; a pass there is CONDITIONAL_DATA_CLASS, never unconditional",
            "the three mandatory slices are separate computations: real_time_only from a standard v5 run on data/raw, "
            "tape_free from a standard v5 run on the reconstructed root floored at the first real vintage, "
            "reconstructed_excluded from the C4 recomputed inside the primary extended run",
            "w1_data_class is REAL_TIME by construction (pre-1996 origins are rejected); "
            "relies_on_reconstructed_origins describes the run, criteria.conditional_data_class the pass",
            "the REAL_TIME-only slice is reporting-only: with 8 recession origins < 12 its C4 is INCONCLUSIVE, not FAIL/PASS",
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
                        help="standard (non-extended) v5 report on data/raw for the REAL_TIME-only C4 slice")
    parser.add_argument("--tape-free-report", action="append", default=None,
                        help="standard (non-extended) v5 report on the reconstructed root (origins floored "
                             "at the first real vintage) for the tape-free C4 slice")
    parser.add_argument("--root", default=str(ROOT), help="repository root for pin resolution")
    parser.add_argument("--json", action="store_true", help="print the report to stdout")
    args = parser.parse_args(argv)
    prereg_path = Path(args.prereg)
    prereg = read_json(prereg_path, "preregistration")
    reports = [Path(p) for p in (args.report or [])]
    standard_paths = [Path(p) for p in (args.standard_report or [])]
    tape_free_paths = [Path(p) for p in (args.tape_free_report or [])]
    try:
        if not reports:
            raise PreregError("at least one --report is required")
        if len(standard_paths) != len(reports) or len(tape_free_paths) != len(reports):
            raise PreregError("each --report needs one --standard-report and one --tape-free-report "
                              f"({len(reports)} primary, {len(standard_paths)} standard, "
                              f"{len(tape_free_paths)} tape-free)")
        pairings = [{"real_time_only": standard_paths[index], "tape_free": tape_free_paths[index]}
                    for index in range(len(reports))]
        report = build_report(prereg, prereg_path, reports, root=Path(args.root),
                              standard_reports=pairings)
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
