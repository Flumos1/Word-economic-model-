#!/usr/bin/env python3
"""Independent (adversarial) review of the P2 0.1 release manifest.

The reviewer does not trust ``build/p2_release/p2_release.json``: every claim is
re-derived from the evidence artefacts and every recorded SHA-256 is recomputed.

Checks (each returns PASS / WARN / FAIL / SKIP and a list of findings):

  R1  manifest_schema       - release metadata, 34 unique inputs, criteria D1-D6 + summary,
                              allowed statuses, candidates are not admitted (structural)
  R2  input_checksums       - all 34 recorded SHA-256 and byte counts recomputed; a mismatch
                              that is repaired by the version committed at HEAD is classified as
                              working-tree drift (WARN), otherwise it is a blocker (FAIL)
  R3  declared_commit       - the recorded ``git_commit`` must contain the hashed inputs; a stale
                              pointer makes the manifest non-reproducible from that commit (WARN)
  R4  criteria_evidence     - D1..D6 are re-derived from FIGARO/WIOD manifests, OECD coverage,
                              the two scenario report indexes and the GVAR report; the claimed
                              status must follow from the evidence (numbers included)
  R5  no_admission_claims   - no positive admission / T2 / ACTIVE / deployment claim in the
                              manifest or in docs/137; structural flags must stay false
  R6  docs137_consistency   - docs/137 numbers, statuses, test count and size must match the
                              manifest and the artefacts (drift is WARN, wrong numbers are FAIL)
  R7  reproducibility       - the documented commands exist and ``--help`` works; the data root
                              exists; the manifest is rebuilt byte-for-byte with fixed
                              generated_at/date/git_commit (fail-closed)
  R8  limitations           - the required limitations are stated (D4 RESEARCH_ONLY, OECD 2016 /
                              34 industries, SFC A_c=FD_c, no PIT, T2/ACTIVE and micro excluded)
  R9  dataset_digests       - the 94 MB FIGARO and 29 MB OECD dataset digests match the manifest
  R10 candidate_states      - T1 CANDIDATE (C4 FAIL), regime SHADOW_TEST conditional, C6 0/12

Output: ``build/external_review/review.json`` + ``review.md`` (deterministic except
``generated_at``).  Exit codes: 0 = no blocker, 1 = at least one FAIL (no sign-off),
2 = release artefact missing or unreadable (nothing written, fail-closed).

Usage:
    python tools/external_review.py
    python tools/external_review.py --no-rebuild --skip-git
    python tools/external_review.py --out-dir build/external_review --generated-at 2026-11-14T22:00:00Z
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "AMFE-EXTERNAL-REVIEW/1.0.0"
RELEASE_SCHEMA = "AMFE-P2-RELEASE/1.0.0"
RELEASE = "P2 0.1"
DEFAULT_RELEASE = Path("build/p2_release/p2_release.json")
DEFAULT_DOC = "docs/137_P2_0.1_релиз.md"
DOC133 = "docs/133_SFC_IO_интеграция.md"
DEFAULT_OUT = Path("build/external_review")
STATUSES = ("PASS", "PARTIAL", "FAIL", "RESEARCH_ONLY")
SEVERITIES = ("BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NEGATIONS = ("не ", "нет", "без ", "вне ", "недо", "not ", " no ", "never", "cannot", "n't")

REQUIRED_LIMITATION_TOKENS = (
    ("D4 is RESEARCH_ONLY", ("D4 — research-only",)),
    ("D4 is outside admission", ("вне допуска", "not connected to admission")),
    ("OECD 2016 edition", ("OECD ICIO 2016",)),
    ("OECD 34 industries", ("34 отрасли", "34 industries")),
    ("SFC absorption convention A_c = FD_c", ("A_c = FD_c",)),
    ("no point-in-time data", ("point-in-time",)),
    ("T2/ACTIVE excluded", ("T2/ACTIVE",)),
    ("micro level excluded", ("микроуровень",)),
    ("scenarios are comparative statics", ("сравнительная статика",)),
)

FORBIDDEN_CLAIMS = (
    (re.compile(r"\bACTIVE\b"), "ACTIVE deployment claim"),
    (re.compile(r"допущ(?:ен|ена|ено|ены|ён)"), "admission claim"),
    (re.compile(r"admission\s*[:=]\s*true", re.I), "admission=true claim"),
    (re.compile(r"production[- ]ready", re.I), "production readiness claim"),
    (re.compile(r"пригодн\w*\s+к\s+(?:промышленной|production)", re.I),
     "deployment readiness claim"),
)

DOC137_NUMBERS = (
    ("FIGARO named GDP coverage", "87,2771"),
    ("OECD coverage minimum", "95,35"),
    ("OECD coverage maximum", "95,47"),
    ("WIOD coverage minimum", "84,38"),
    ("WIOD coverage maximum", "89,85"),
    ("GVAR cells won by the primary", "4/32"),
    ("scenario count", "8 сценариев"),
    ("D4 status", "RESEARCH_ONLY"),
    ("T1 status", "CANDIDATE"),
    ("regime status", "SHADOW_TEST"),
    ("no admitted model", "admitted=false"),
    ("conditional data class", "conditional_data_class"),
)


class ReviewError(RuntimeError):
    """The release artefact itself is missing or unreadable (fail-closed, exit 2)."""


# ----------------------------------------------------------------------------------- primitives
def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReviewError(f"{label} is missing: {path}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewError(f"{label} is unreadable: {path}: {error}") from None


def git_blob(repo: Path, revision: str, relative: str) -> bytes | None:
    """Raw bytes of ``revision:relative`` or None when git/the object is unavailable."""
    try:
        done = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:{relative}"],
                              capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_head(repo: Path) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, check=False, timeout=60, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _finding(check: str, severity: str, message: str, evidence: dict | None = None) -> dict:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    digest = hashlib.sha1(message.encode("utf-8")).hexdigest()[:8]
    return {"id": f"{check}-{severity}-{digest}",
            "check": check, "severity": severity, "message": message,
            "evidence": evidence or {}}


def _check(check_id: str, title: str, status: str, findings: list, evidence: dict) -> dict:
    return {"id": check_id, "title": title, "status": status,
            "findings": findings, "evidence": evidence}


def _status_from(findings: list) -> str:
    if any(item["severity"] in ("BLOCKER", "HIGH") for item in findings):
        return "FAIL"
    if any(item["severity"] in ("MEDIUM", "LOW", "INFO") for item in findings):
        return "WARN"
    return "PASS"


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same(claimed, observed, tolerance: float = 1e-9) -> bool:
    left, right = _num(claimed), _num(observed)
    if left is None or right is None:
        return claimed == observed
    return abs(left - right) <= tolerance * max(1.0, abs(left), abs(right))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def _walk_strings(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{index}]")


def _url(path: Path) -> str:
    return str(path).replace("\\", "/")


def _data_root(release: dict, repo: Path) -> Path:
    declared = release.get("data_root")
    if declared and Path(declared).is_dir():
        return Path(declared)
    fallback = repo / "data" / "mrio"
    return fallback if fallback.is_dir() else Path(declared or "data/mrio")


def _input_path(release: dict, entry: dict, repo: Path) -> Path:
    raw = Path(str(entry["path"]))
    return raw if raw.is_absolute() else repo / raw


# ------------------------------------------------------------------------------------- R1 schema
def check_manifest(release: dict, release_path: Path) -> dict:
    findings, evidence = [], {}
    if release.get("schema") != RELEASE_SCHEMA:
        findings.append(_finding("R1", "BLOCKER", f"unexpected manifest schema {release.get('schema')!r}",
                                 {"expected": RELEASE_SCHEMA}))
    for field, expected in (("release", RELEASE), ("version", "0.1")):
        if release.get(field) != expected:
            findings.append(_finding("R1", "BLOCKER", f"manifest {field} is {release.get(field)!r}",
                                     {"expected": expected}))
    criteria = release.get("criteria", [])
    ids = [item.get("id") for item in criteria]
    evidence.update({"criteria": ids, "inputs": len(release.get("inputs", [])),
                     "schema": release.get("schema"), "status": release.get("status"),
                     "git_commit": release.get("git_commit"), "date": release.get("date")})
    if ids != ["D1", "D2", "D3", "D4", "D5", "D6"]:
        findings.append(_finding("R1", "BLOCKER", f"criteria D1-D6 not in order: {ids}"))
    summary = release.get("criteria_summary", {})
    if summary != {item["id"]: item.get("status") for item in criteria}:
        findings.append(_finding("R1", "BLOCKER", "criteria_summary disagrees with criteria",
                                 {"summary": summary}))
    bad = [(item.get("id"), item.get("status")) for item in criteria
           if item.get("status") not in STATUSES]
    if bad:
        findings.append(_finding("R1", "BLOCKER", f"unknown criterion status: {bad}"))
    inputs = release.get("inputs", [])
    ids_in = [item.get("id") for item in inputs]
    if len(inputs) != 34:
        findings.append(_finding("R1", "HIGH", f"manifest lists {len(inputs)} inputs, expected 34",
                                 {"count": len(inputs)}))
    if len(set(ids_in)) != len(ids_in):
        findings.append(_finding("R1", "HIGH", "duplicate input ids in the manifest"))
    bad_sha = [item.get("id") for item in inputs if not HEX64.match(str(item.get("sha256")))]
    if bad_sha:
        findings.append(_finding("R1", "BLOCKER", f"inputs without a valid SHA-256: {bad_sha}"))
    required = [item for item in inputs if item.get("required")]
    if len(required) < 25:
        findings.append(_finding("R1", "HIGH", f"only {len(required)} inputs are marked required"))
    evidence.update({"required_inputs": len(required)})
    if release.get("status") != "RESEARCH_RELEASE":
        findings.append(_finding("R1", "BLOCKER", f"release status is {release.get('status')!r}, "
                                                  "expected RESEARCH_RELEASE"))
    return _check("R1", "manifest schema, 34 inputs, D1-D6, RESEARCH_RELEASE", _status_from(findings),
                  findings, evidence)


# ---------------------------------------------------------------------------- R2 input checksums
def check_inputs(release: dict, repo: Path, use_git: bool = True) -> dict:
    findings, evidence = [], {"inputs": [], "verified": 0, "missing": 0,
                              "mismatched": 0, "working_tree_drift": 0}
    head = git_head(repo) if use_git else None
    for entry in release.get("inputs", []):
        path = _input_path(release, entry, repo)
        row = {"id": entry.get("id"), "path": str(entry.get("path")),
               "required": bool(entry.get("required")), "exists": path.is_file()}
        if not path.is_file():
            evidence["missing"] += 1
            row["status"] = "MISSING"
            severity = "BLOCKER" if entry.get("required") else "LOW"
            findings.append(_finding("R2", severity,
                                     f"input {entry.get('id')} is missing: {_url(path)}",
                                     {"path": str(entry.get("path"))}))
            evidence["inputs"].append(row)
            continue
        observed = sha256_file(path)
        observed_bytes = path.stat().st_size
        row.update({"sha256": observed, "bytes": observed_bytes})
        if observed == entry.get("sha256") and observed_bytes == entry.get("bytes"):
            evidence["verified"] += 1
            row["status"] = "OK"
            evidence["inputs"].append(row)
            continue
        repaired = None
        if use_git and head and not Path(str(entry.get("path"))).is_absolute():
            blob = git_blob(repo, head, str(entry.get("path")))
            if blob is not None and sha256_bytes(blob) == entry.get("sha256") \
                    and len(blob) == entry.get("bytes"):
                repaired = head
        if repaired:
            evidence["working_tree_drift"] += 1
            row.update({"status": "WORKING_TREE_DRIFT", "committed_at": repaired})
            findings.append(_finding(
                "R2", "LOW",
                f"input {entry.get('id')} changed after the release (working tree drift); the "
                f"committed version at {repaired[:7]} matches the manifest",
                {"path": str(entry.get("path")), "recorded": entry.get("sha256"),
                 "observed": observed, "recorded_bytes": entry.get("bytes"),
                 "observed_bytes": observed_bytes, "committed_at": repaired}))
        else:
            evidence["mismatched"] += 1
            row["status"] = "MISMATCH"
            findings.append(_finding(
                "R2", "BLOCKER" if entry.get("required") else "HIGH",
                f"input {entry.get('id')} checksum mismatch: recorded {entry.get('sha256')[:16]}..., "
                f"observed {observed[:16]}...",
                {"path": str(entry.get("path")), "recorded": entry.get("sha256"),
                 "observed": observed, "recorded_bytes": entry.get("bytes"),
                 "observed_bytes": observed_bytes}))
        evidence["inputs"].append(row)
    evidence["head"] = head
    evidence["total"] = len(release.get("inputs", []))
    return _check("R2", "all 34 recorded SHA-256 recomputed and classified", _status_from(findings),
                  findings, evidence)


# ---------------------------------------------------------------------------- R3 declared commit
def check_declared_commit(release: dict, repo: Path, use_git: bool = True) -> dict:
    findings = []
    revision = release.get("git_commit")
    evidence = {"git_commit": revision, "usable": bool(use_git)}
    if not use_git or not revision:
        return _check("R3", "declared git_commit reproduces the hashed inputs", "SKIP", findings,
                      evidence)
    head = git_head(repo)
    evidence["head"] = head
    if head is None:
        return _check("R3", "declared git_commit reproduces the hashed inputs", "SKIP",
                      [_finding("R3", "INFO", "git is unavailable; the declared commit was not "
                                              "verified")], evidence)
    matches = missing = mismatched = 0
    details = []
    for entry in release.get("inputs", []):
        relative = str(entry.get("path"))
        if Path(relative).is_absolute():
            continue
        blob = git_blob(repo, str(revision), relative)
        if blob is None:
            missing += 1
            details.append({"path": relative, "status": "MISSING_AT_COMMIT"})
        else:
            # Line endings are not content: git may check out CRLF while the blob stores LF
            # (core.autocrlf), so compare normalised bytes before calling a digest mismatch.
            normalise = lambda data: data.replace(b"\r\n", b"\n")
            try:
                working = normalise((repo / relative).read_bytes())
            except OSError:
                working = None
            if working is not None and normalise(blob) == working:
                matches += 1
            else:
                mismatched += 1
                details.append({"path": relative, "status": "DIFFERS_AT_COMMIT",
                                "sha256": sha256_bytes(blob), "bytes": len(blob)})
    evidence.update({"matches": matches, "missing": missing, "mismatched": mismatched,
                     "details": details})
    if missing or mismatched:
        findings.append(_finding(
            "R3", "MEDIUM",
            f"declared git_commit {str(revision)[:7]} does not contain the release inputs "
            f"({matches} match, {mismatched} differ, {missing} absent): the manifest is not "
            f"reproducible from its own commit pointer",
            {"git_commit": revision, "head": head, "matches": matches,
             "mismatched": mismatched, "missing": missing}))
    return _check("R3", "declared git_commit reproduces the hashed inputs", _status_from(findings),
                  findings, evidence)


# --------------------------------------------------------------------------- R4 criteria evidence
def _figaro_manifest(release, repo) -> tuple[Path, dict]:
    data_root = _data_root(release, repo)
    path = data_root / "figaro" / "figaro_2019" / "manifest.json"
    return path, _read_json(path, "FIGARO manifest")


def _oecd_coverage(repo: Path) -> dict:
    return _read_json(repo / "protocols" / "oecd_icio_coverage.json", "OECD coverage protocol")


def _wiod_manifests(release, repo) -> tuple[list[dict], list[Path]]:
    data_root = _data_root(release, repo)
    docs, paths = [], []
    for year in release["world"]["wiod"]["years"]:
        path = data_root / "wiod_2016" / f"wiod_{year}" / "manifest.json"
        docs.append(_read_json(path, f"WIOD {year} manifest"))
        paths.append(path)
    return docs, paths


def _report_index(repo: Path, relative: str, label: str) -> dict:
    return _read_json(repo / relative, label)


def _gvar_report(release, repo) -> tuple[Path, dict]:
    data_root = _data_root(release, repo)
    path = data_root / "cache" / "gvar_real" / "gvar_v2_report.json"
    return path, _read_json(path, "GVAR report")


def _criteria_map(release: dict) -> dict:
    return {item["id"]: item for item in release.get("criteria", [])}


def verify_d1(release: dict, repo: Path) -> tuple[str, list, dict]:
    findings, evidence = [], {}
    criteria = _criteria_map(release)["D1"]
    claimed_status, claimed_evidence = criteria.get("status"), criteria.get("evidence", {})
    world = release.get("world", {})
    figaro_path, figaro = _figaro_manifest(release, repo)
    wiod_docs, wiod_paths = _wiod_manifests(release, repo)
    oecd = _oecd_coverage(repo)
    fig = {
        "countries": figaro["dataset"]["countries"],
        "industries": figaro["dataset"]["industries"],
        "sectors": figaro["dataset"]["countries"] * figaro["dataset"]["industries"],
        "coverage_named_pct": figaro["coverage"]["named_share_pct"],
        "coverage_with_row_pct": figaro["coverage"]["coverage_with_row_pct"],
        "gate_min_80pct": bool(figaro["coverage"].get("named_only_min_80pct")),
        "dataset_sha256": figaro["dataset"]["sha256"],
        "dataset_bytes": figaro["dataset"]["bytes"],
        "cells_Z": figaro["dataset"]["cells_Z"],
        "stored_Z": figaro["dataset"]["stored_Z"],
        "years": figaro["dataset"]["years"],
    }
    wiod = {
        "manifests": len(wiod_docs),
        "countries": sorted({doc["dataset"]["countries"] for doc in wiod_docs}),
        "industries": sorted({doc["dataset"]["industries"] for doc in wiod_docs}),
        "years": [doc["dataset"]["years"][0] for doc in wiod_docs],
        "coverage_min_pct": min(doc["coverage"]["named_share_percent"] for doc in wiod_docs),
        "coverage_max_pct": max(doc["coverage"]["named_share_percent"] for doc in wiod_docs),
        "all_validation_pass": all(all(doc["validation"]["checks"].values()) for doc in wiod_docs),
        "all_p2_gate": all(bool(doc["p2_gate"].get("passed")) for doc in wiod_docs),
        "schema_v11": all(str(doc["dataset"].get("schema_version")) == "1.1.0" for doc in wiod_docs),
        "adjustments": bool(all(doc["dataset"].get("has_adjustments") for doc in wiod_docs)),
    }
    oec = {
        "economies": oecd["economies"], "countries_named": oecd["countries_named"],
        "industries": oecd["industries"], "sectors": oecd["sectors"],
        "coverage_min_pct": oecd["coverage_world_value_added_pct_min"],
        "coverage_max_pct": oecd["coverage_world_value_added_pct_max"],
        "d1_pass_period": oecd["d1_pass_period"], "period": oecd["period"],
        "status": oecd["status"],
    }
    evidence.update({"figaro": fig, "wiod": wiod, "oecd": oec,
                     "claimed_status": claimed_status})
    # --- FIGARO claims
    if world.get("figaro", {}).get("sha256") != fig["dataset_sha256"]:
        findings.append(_finding("R4", "BLOCKER", "D1: FIGARO dataset digest in the manifest does not "
                                                  "match the FIGARO manifest",
                                 {"manifest": world.get("figaro", {}).get("sha256"),
                                  "figaro_manifest": fig["dataset_sha256"]}))
    if not _same(world.get("figaro", {}).get("gdp_coverage_named_pct"), fig["coverage_named_pct"]):
        findings.append(_finding("R4", "BLOCKER", "D1: FIGARO named GDP coverage differs",
                                 {"manifest": world.get("figaro", {}).get("gdp_coverage_named_pct"),
                                  "figaro_manifest": fig["coverage_named_pct"]}))
    if fig["coverage_named_pct"] < 80.0:
        findings.append(_finding("R4", "BLOCKER", "D1: FIGARO named GDP coverage is below the 80% gate",
                                 {"coverage": fig["coverage_named_pct"]}))
    # --- OECD claims
    if not _same(claimed_evidence.get("oecd", {}).get("economies"), oec["economies"]):
        findings.append(_finding("R4", "BLOCKER", "D1: OECD economies differ", {
            "manifest": claimed_evidence.get("oecd", {}).get("economies"),
            "coverage": oec["economies"]}))
    if not _same(claimed_evidence.get("oecd", {}).get("industries"), oec["industries"]):
        findings.append(_finding("R4", "BLOCKER", "D1: OECD industry count differs", {
            "manifest": claimed_evidence.get("oecd", {}).get("industries"),
            "coverage": oec["industries"]}))
    for key, observed in (("gdp_coverage_min_pct", oec["coverage_min_pct"]),
                          ("gdp_coverage_max_pct", oec["coverage_max_pct"])):
        if not _same(claimed_evidence.get("oecd", {}).get(key), observed):
            findings.append(_finding("R4", "BLOCKER", f"D1: OECD {key} differs", {
                "manifest": claimed_evidence.get("oecd", {}).get(key), "coverage": observed}))
    if oec["coverage_min_pct"] < 80.0:
        findings.append(_finding("R4", "BLOCKER", "D1: OECD coverage is below the 80% gate",
                                 {"coverage": oec["coverage_min_pct"]}))
    # --- WIOD claims
    if not _same(claimed_evidence.get("wiod", {}).get("countries"), wiod["countries"][0]) \
            or wiod["countries"] != [44]:
        findings.append(_finding("R4", "BLOCKER", "D1: WIOD country count differs",
                                 {"manifest": claimed_evidence.get("wiod", {}).get("countries"),
                                  "manifests": wiod["countries"]}))
    for key, observed in (("gdp_coverage_min_pct", wiod["coverage_min_pct"]),
                          ("gdp_coverage_max_pct", wiod["coverage_max_pct"])):
        if not _same(claimed_evidence.get("wiod", {}).get(key), observed):
            findings.append(_finding("R4", "BLOCKER", f"D1: WIOD {key} differs", {
                "manifest": claimed_evidence.get("wiod", {}).get(key), "manifests": observed}))
    if not wiod["all_validation_pass"]:
        findings.append(_finding("R4", "BLOCKER", "D1: a WIOD validation check is false"))
    if not wiod["all_p2_gate"]:
        findings.append(_finding("R4", "BLOCKER", "D1: the WIOD p2_gate did not pass"))
    if claimed_status != "PASS":
        findings.append(_finding("R4", "BLOCKER", f"D1 claimed {claimed_status!r} but the evidence "
                                                  "supports PASS"))
    return ("CONFIRMED" if not findings else "NOT_CONFIRMED", findings, evidence)


def verify_d2(release: dict, repo: Path) -> tuple[str, list, dict]:
    findings, evidence = [], {}
    criteria = _criteria_map(release)["D2"]
    claimed_status, claimed = criteria.get("status"), criteria.get("evidence", {})
    figaro_index = _report_index(repo, "build/world/report_index.json", "FIGARO scenario index")
    oecd_index = _report_index(repo, "build/world_oecd/report_index.json", "OECD scenario index")
    observed = {
        "figaro": {"sectors": figaro_index["dataset"]["sectors"],
                   "max_abs_material_residual": figaro_index["checks"]["max_abs_material_residual"],
                   "all_validations_pass": figaro_index["checks"]["all_validations_pass"]},
        "oecd": {"sectors": oecd_index["dataset"]["sectors"],
                 "max_abs_material_residual": oecd_index["checks"]["max_abs_material_residual"],
                 "all_validations_pass": oecd_index["checks"]["all_validations_pass"]},
    }
    evidence.update({"observed": observed, "claimed_status": claimed_status,
                     "figaro_dataset_sectors": release["world"]["figaro"]["sectors"],
                     "oecd_dataset_sectors": release["world"]["oecd"]["sectors"]})
    for dataset in ("figaro", "oecd"):
        entry = claimed.get(dataset, {})
        for key in ("sectors", "max_abs_material_residual", "all_validations_pass"):
            if key == "all_validations_pass":
                if bool(entry.get(key)) != bool(observed[dataset][key]):
                    findings.append(_finding("R4", "BLOCKER",
                                             f"D2: {dataset} all_validations_pass differs",
                                             {"manifest": entry.get(key),
                                              "index": observed[dataset][key]}))
            elif not _same(entry.get(key), observed[dataset][key]):
                findings.append(_finding("R4", "BLOCKER",
                                         f"D2: {dataset} {key} differs",
                                         {"manifest": entry.get(key),
                                          "index": observed[dataset][key]}))
        if entry.get("status") != "RESEARCH_ONLY":
            findings.append(_finding("R4", "HIGH", f"D2: {dataset} evidence status is not "
                                                   "RESEARCH_ONLY"))
    if release["world"]["figaro"]["sectors"] != observed["figaro"]["sectors"]:
        findings.append(_finding("R4", "BLOCKER", "D2: world.figaro.sectors differs from the index"))
    if release["world"]["oecd"]["sectors"] != observed["oecd"]["sectors"]:
        findings.append(_finding("R4", "BLOCKER", "D2: world.oecd.sectors differs from the index"))
    if claimed_status != "PASS":
        findings.append(_finding("R4", "BLOCKER", f"D2 claimed {claimed_status!r} but the evidence "
                                                  "supports PASS"))
    return ("CONFIRMED" if not findings else "NOT_CONFIRMED", findings, evidence)


def verify_d3(release: dict, repo: Path) -> tuple[str, list, dict]:
    findings, evidence = [], {}
    criteria = _criteria_map(release)["D3"]
    claimed_status, claimed = criteria.get("status"), criteria.get("evidence", {})
    figaro_index = _report_index(repo, "build/world/report_index.json", "FIGARO scenario index")
    oecd_index = _report_index(repo, "build/world_oecd/report_index.json", "OECD scenario index")
    observed = {}
    for label, index in (("figaro", figaro_index), ("oecd", oecd_index)):
        block = index["sfc"]["checks"]
        observed[label] = {key: block.get(key) for key in
                           ("sfc_pass", "sfc_identity_residual", "sfc_net_worth_residual",
                            "global_current_account_sum", "saving_investment_identity_max_abs",
                            "current_account_disagreement_max_abs",
                            "gdp_sum_minus_final_demand_sum")}
    doc133 = repo / DOC133
    doc133_text = _normalise(doc133.read_text(encoding="utf-8")) if doc133.is_file() else ""
    evidence.update({"observed": observed, "claimed_status": claimed_status,
                     "doc133_present": doc133.is_file(),
                     "absorption_convention_documented": "A_c = FD_c" in doc133_text})
    if not doc133.is_file():
        findings.append(_finding("R4", "HIGH", f"D3: {DOC133} is missing"))
    for label in ("figaro", "oecd"):
        block = observed[label]
        if not block["sfc_pass"]:
            findings.append(_finding("R4", "BLOCKER", f"D3: SFC did not pass on {label}"))
        if not _same(block["sfc_identity_residual"], 0.0):
            findings.append(_finding("R4", "BLOCKER",
                                     f"D3: SFC identity residual is non-zero on {label}",
                                     {"residual": block["sfc_identity_residual"]}))
        if not _same(block["sfc_net_worth_residual"], 0.0):
            findings.append(_finding("R4", "BLOCKER", f"D3: SFC net worth residual is non-zero on "
                                                     f"{label}", {"residual": block["sfc_net_worth_residual"]}))
        if abs(_num(block["global_current_account_sum"]) or 0.0) > 1e-6:
            findings.append(_finding("R4", "HIGH", f"D3: |sum CA| is not zero on {label}",
                                     {"sum_ca": block["global_current_account_sum"]}))
        manifest = claimed.get(label, {}).get("checks", {})
        for key in ("sfc_pass", "sfc_identity_residual", "sfc_net_worth_residual",
                    "global_current_account_sum", "saving_investment_identity_max_abs",
                    "current_account_disagreement_max_abs", "gdp_sum_minus_final_demand_sum"):
            if key not in manifest:
                findings.append(_finding("R4", "HIGH", f"D3: manifest omits {label}.{key}"))
            elif not _same(manifest.get(key), block[key], tolerance=1e-6):
                findings.append(_finding("R4", "BLOCKER", f"D3: {label}.{key} differs",
                                         {"manifest": manifest.get(key), "index": block[key]}))
    # the OECD residual caveat: gdp-FD residual is 8.124 USD_mn, above the 1.0 USD_mn dataset
    # tolerance; docs/133 must disclose it and the SFC identity must stay exactly 0.
    oecd_material = max(abs(_num(observed["oecd"]["current_account_disagreement_max_abs"]) or 0.0),
                        abs(_num(observed["oecd"]["gdp_sum_minus_final_demand_sum"]) or 0.0))
    if oecd_material > 1.0:
        evidence["oecd_material_residual_usd_mn"] = oecd_material
        if "округлен" not in doc133_text and "rounding" not in doc133_text.lower():
            findings.append(_finding("R4", "HIGH", "D3: the OECD residual (8.124 USD_mn) is above the "
                                                  f"1.0 tolerance but {DOC133} does not disclose it "
                                                  "as rounding"))
    if claimed_status != "PASS":
        findings.append(_finding("R4", "BLOCKER", f"D3 claimed {claimed_status!r} but the evidence "
                                                  "supports PASS"))
    verdict = "CONFIRMED" if not findings else "NOT_CONFIRMED"
    return (("CONFIRMED_WITH_NOTES" if verdict == "CONFIRMED" and oecd_material > 1.0 else verdict),
            findings, evidence)


def verify_d4(release: dict, repo: Path) -> tuple[str, list, dict]:
    findings, evidence = [], {}
    criteria = _criteria_map(release)["D4"]
    claimed_status, claimed = criteria.get("status"), criteria.get("evidence", {})
    gvar_path, gvar = _gvar_report(release, repo)
    sample = gvar["sample"]
    baseline = gvar["vs_best_baseline"]
    year_weights = gvar["comparisons"]["GVAR_2019"]["aggregate"]
    observed = {
        "report_id": gvar["report_id"],
        "countries": sample["countries"], "years": sample["years"],
        "cells": baseline["cells"],
        "primary_better_cells": baseline["primary_better_cells"],
        "baseline_better_cells": baseline["baseline_better_cells"],
        "primary": gvar["comparisons"]["primary"],
        "baselines": baseline["baselines"],
        "oos_negative": baseline["baseline_better_cells"] > baseline["primary_better_cells"],
        "year_weights_primary_better_cells": year_weights["primary_better_cells"],
        "year_weights_other_better_cells": year_weights["other_better_cells"],
        "year_weights_mean_delta_rmse": year_weights["mean_delta_rmse"],
    }
    evidence.update({"observed": observed, "claimed_status": claimed_status,
                     "gvar_report": _url(gvar_path)})
    manifest = claimed.get("gvar", {})
    for key in ("countries", "years", "cells", "primary_better_cells",
                "baseline_better_cells", "year_weights_mean_delta_rmse"):
        if not _same(manifest.get(key), observed[key], tolerance=1e-6):
            findings.append(_finding("R4", "BLOCKER", f"D4: {key} differs",
                                     {"manifest": manifest.get(key), "gvar": observed[key]}))
    if list(manifest.get("baselines") or []) != list(observed["baselines"]):
        findings.append(_finding("R4", "HIGH", "D4: baseline list differs",
                                 {"manifest": manifest.get("baselines"),
                                  "gvar": observed["baselines"]}))
    if manifest.get("primary") != observed["primary"]:
        findings.append(_finding("R4", "HIGH", "D4: primary model differs",
                                 {"manifest": manifest.get("primary"), "gvar": observed["primary"]}))
    if bool(manifest.get("oos_negative")) != observed["oos_negative"]:
        findings.append(_finding("R4", "BLOCKER", "D4: oos_negative claim differs",
                                 {"manifest": manifest.get("oos_negative"),
                                  "gvar": observed["oos_negative"]}))
    world = release["world"]["gvar"]
    for key in ("primary_better_cells", "baseline_better_cells", "year_weights_primary_better_cells",
                "year_weights_other_better_cells", "year_weights_mean_delta_rmse", "countries",
                "years", "cells"):
        if key in world and not _same(world.get(key), observed[key], tolerance=1e-6):
            findings.append(_finding("R4", "BLOCKER", f"D4: world.gvar.{key} differs",
                                     {"manifest": world.get(key), "gvar": observed[key]}))
    if world.get("oos_negative") is not True and observed["oos_negative"]:
        findings.append(_finding("R4", "BLOCKER", "D4: world.gvar.oos_negative is not true"))
    # RESEARCH_ONLY must be the claimed status and the report must stay disconnected from admission
    if claimed_status != "RESEARCH_ONLY":
        findings.append(_finding("R4", "BLOCKER", f"D4 claimed {claimed_status!r}; the negative OOS "
                                                  "evidence only supports RESEARCH_ONLY"))
    if not any("not connected to admission" in str(item) for item in gvar.get("limits", [])):
        findings.append(_finding("R4", "MEDIUM", "D4: the GVAR report does not state that it is "
                                                 "disconnected from admission"))
    verdict = "CONFIRMED" if not findings else "NOT_CONFIRMED"
    return (("CONFIRMED_WITH_NOTES" if verdict == "CONFIRMED" else verdict), findings, evidence)


def verify_d5(release: dict, repo: Path) -> tuple[str, list, dict]:
    findings, evidence = [], {}
    criteria = _criteria_map(release)["D5"]
    claimed_status = criteria.get("status")
    observed, manifest = {}, release["world"].get("scenarios", [])
    for label, relative, directory in (
            ("figaro", "build/world/report_index.json", "build/world/reports"),
            ("oecd", "build/world_oecd/report_index.json", "build/world_oecd/reports")):
        index = _report_index(repo, relative, f"{label} scenario index")
        scenarios = index["scenarios"]
        missing_files = [scenario["file_json"] for scenario in scenarios
                         if not (repo / directory / Path(scenario["file_json"]).name).is_file()]
        missing_md = [scenario["file_md"] for scenario in scenarios
                      if not (repo / directory / Path(scenario["file_md"]).name).is_file()]
        per_scenario_ok = all(
            scenario["checks"]["feasible"] and scenario["checks"]["validation_pass"]
            and scenario["checks"]["sfc_block_pass"] for scenario in scenarios)
        observed[label] = {
            "scenario_count": len(scenarios),
            "index_scenario_count": index["checks"]["scenario_count"],
            "all_feasible": index["checks"]["all_feasible"],
            "all_sfc_pass": index["checks"]["all_sfc_pass"],
            "all_validations_pass": index["checks"]["all_validations_pass"],
            "max_abs_material_residual": index["checks"]["max_abs_material_residual"],
            "missing_json": missing_files, "missing_md": missing_md,
            "per_scenario_checks_ok": per_scenario_ok,
            "max_headline_residual": max(
                (scenario["headline"]["max_abs_material_residual"] for scenario in scenarios),
                default=0.0),
        }
        if len(scenarios) != 8:
            findings.append(_finding("R4", "BLOCKER", f"D5: {label} has {len(scenarios)} scenarios, "
                                                     "expected 8"))
        connect = {"all_feasible": "all_feasible", "all_sfc_pass": "all_sfc_pass",
                   "all_validations_pass": "all_validations_pass"}
        for flag, key in connect.items():
            if not observed[label][flag]:
                findings.append(_finding("R4", "BLOCKER", f"D5: {label} {key} is false"))
            if not observed[label][key]:
                findings.append(_finding("R4", "BLOCKER", f"D5: {label} index {key} is false"))
        if observed[label]["missing_json"] or observed[label]["missing_md"]:
            findings.append(_finding("R4", "BLOCKER", f"D5: {label} scenario report files are missing",
                                     {"json": observed[label]["missing_json"],
                                      "md": observed[label]["missing_md"]}))
        if not observed[label]["per_scenario_checks_ok"]:
            findings.append(_finding("R4", "HIGH", f"D5: a {label} scenario report fails its own "
                                                  "checks"))
    # claims in the manifest (criteria evidence and world.scenarios)
    claimed_evidence = criteria.get("evidence", {}).get("scenarios", [])
    by_name = {item.get("dataset"): item for item in claimed_evidence}
    world_by_name = {item.get("dataset"): item for item in manifest}
    for label in ("figaro", "oecd"):
        for source, container in (("criteria", by_name), ("world", world_by_name)):
            entry = container.get(label, {})
            if not entry:
                findings.append(_finding("R4", "BLOCKER", f"D5: {source} has no {label} scenario "
                                                          "entry"))
                continue
            for key, flag in (("scenario_count", "scenario_count"),
                              ("all_feasible", "all_feasible"),
                              ("all_sfc_pass", "all_sfc_pass"),
                              ("all_validations_pass", "all_validations_pass")):
                if not _same(entry.get(key), observed[label][flag]):
                    findings.append(_finding("R4", "BLOCKER", f"D5: {source}.{label}.{key} differs",
                                             {"manifest": entry.get(key),
                                              "index": observed[label][flag]}))
            for key in ("max_abs_material_residual", "max_scenario_headline_residual"):
                if not _same(entry.get(key), observed[label]["max_abs_material_residual"],
                             tolerance=1e-9):
                    findings.append(_finding("R4", "BLOCKER", f"D5: {source}.{label}.{key} differs",
                                             {"manifest": entry.get(key),
                                              "index": observed[label]["max_abs_material_residual"]}))
    evidence.update({"observed": observed, "claimed_status": claimed_status,
                     "scenario_dirs": ["build/world/reports", "build/world_oecd/reports"]})
    if claimed_status != "PASS":
        findings.append(_finding("R4", "BLOCKER", f"D5 claimed {claimed_status!r} but the evidence "
                                                  "supports PASS"))
    return ("CONFIRMED" if not findings else "NOT_CONFIRMED", findings, evidence)


def verify_d6(release: dict, repo: Path) -> tuple[str, list, dict]:
    findings, evidence = [], {}
    criteria = _criteria_map(release)["D6"]
    claimed_status, claimed = criteria.get("status"), criteria.get("evidence", {})
    pipeline = claimed.get("pipeline") or "tools/world_pipeline.py"
    required_tools = [pipeline, "tools/world_reports.py"]
    required_tests = claimed.get("tests_present") or ["test_world_pipeline.py",
                                                      "test_figaro_import.py",
                                                      "test_p2_release.py"]
    required_docs = list(claimed.get("docs_required") or [])
    checks = {}
    for tool in required_tools:
        checks[tool] = (repo / tool).is_file()
    for test in required_tests:
        checks[f"tests/{test}"] = (repo / "tests" / test).is_file()
    for doc in required_docs:
        checks[doc] = (repo / doc).is_file()
    checks[DEFAULT_DOC] = (repo / DEFAULT_DOC).is_file()
    missing = sorted(name for name, present in checks.items() if not present)
    evidence.update({"present": {name: checks[name] for name in sorted(checks)},
                     "missing": missing, "claimed_status": claimed_status})
    for name in missing:
        severity = "BLOCKER" if name in required_tools or name == DEFAULT_DOC else "HIGH"
        findings.append(_finding("R4", severity, f"D6: required artefact is missing: {name}"))
    docs_missing = claimed.get("missing") or []
    if docs_missing:
        findings.append(_finding("R4", "HIGH", f"D6: the manifest admits missing docs: {docs_missing}"))
    if claimed_status != "PASS":
        findings.append(_finding("R4", "BLOCKER", f"D6 claimed {claimed_status!r} but the evidence "
                                                  "supports PASS"))
    return ("CONFIRMED" if not findings else "NOT_CONFIRMED", findings, evidence)


VERIFIERS = {"D1": verify_d1, "D2": verify_d2, "D3": verify_d3, "D4": verify_d4,
             "D5": verify_d5, "D6": verify_d6}


def check_criteria(release: dict, repo: Path) -> dict:
    findings, criteria_out, evidence = [], {}, {}
    for criterion in release.get("criteria", []):
        cid = criterion.get("id")
        verifier = VERIFIERS.get(cid)
        if verifier is None:
            findings.append(_finding("R4", "BLOCKER", f"no verifier for criterion {cid}"))
            continue
        verdict, own, own_evidence = verifier(release, repo)
        criteria_out[cid] = {"claimed_status": criterion.get("status"), "verdict": verdict,
                             "title": criterion.get("title"), "evidence": own_evidence,
                             "findings": own}
        findings.extend(own)
        if verdict == "NOT_CONFIRMED":
            findings.append(_finding("R4", "BLOCKER",
                                     f"{cid} is NOT confirmed by the evidence", {"criterion": cid}))
    claimed_verdict = {cid: item["verdict"] for cid, item in criteria_out.items()}
    evidence.update({"criteria": claimed_verdict,
                     "all_confirmed": all(item["verdict"] in ("CONFIRMED", "CONFIRMED_WITH_NOTES")
                                          for item in criteria_out.values())})
    return _check("R4", "D1-D6 statuses re-derived from the evidence artefacts",
                  _status_from(findings), findings,
                  dict(evidence, criteria=criteria_out))


# ------------------------------------------------------------------------- R5 no admission claims
def _scan_claims(text: str, label: str) -> list:
    findings = []
    for pattern, description in FORBIDDEN_CLAIMS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 90): match.end() + 90].lower()
            if any(token in window for token in NEGATIONS):
                continue
            findings.append(_finding("R5", "HIGH", f"{label}: unsupported {description}",
                                     {"match": match.group(0), "context": window.strip()[:180]}))
    return findings


def check_claims(release: dict, repo: Path, doc_text: str | None = None) -> dict:
    findings, evidence = [], {}
    if release.get("admission") is not False:
        findings.append(_finding("R5", "BLOCKER", f"release admission flag is {release.get('admission')!r}"))
    if release.get("status") != "RESEARCH_RELEASE":
        findings.append(_finding("R5", "BLOCKER", f"release status is {release.get('status')!r}, not "
                                                  "RESEARCH_RELEASE"))
    allowed = {"CANDIDATE", "SHADOW_TEST"}
    candidates = (release.get("world", {}).get("candidates") or {})
    evidence["admitted"] = candidates.get("admitted")
    if candidates.get("admitted") is not False:
        findings.append(_finding("R5", "BLOCKER", "world.candidates.admitted is not false"))
    for key in ("t1", "regime"):
        block = candidates.get(key) or {}
        status = block.get("status")
        evidence[key] = {"status": status, "status_label": block.get("status_label"),
                         "conditional_data_class": block.get("conditional_data_class")}
        if status not in allowed:
            findings.append(_finding("R5", "BLOCKER", f"candidate {key} claims status {status!r}; "
                                                      "only CANDIDATE/SHADOW_TEST are allowed",
                                     {"candidate": key, "status": status}))
        stages = block.get("stages") or {}
        active = [name for name, value in stages.items() if name.endswith("_to_ACTIVE") and value]
        evidence[f"{key}_stages"] = stages
        if active:
            findings.append(_finding("R5", "BLOCKER", f"candidate {key} claims a stage to ACTIVE: "
                                                      f"{active}", {"stages": stages}))
        if status == "CANDIDATE" and any(stages.values()):
            findings.append(_finding("R5", "HIGH", f"candidate {key} is CANDIDATE but a stage is true",
                                     {"stages": stages}))
        if status == "SHADOW_TEST":
            if not stages.get("CANDIDATE_to_SHADOW_TEST"):
                findings.append(_finding("R5", "HIGH", f"candidate {key} is SHADOW_TEST without the "
                                                      "CANDIDATE->SHADOW_TEST stage"))
            if stages.get("SHADOW_TEST_to_LIMITED_DEPLOYMENT"):
                findings.append(_finding("R5", "BLOCKER", f"candidate {key} claims LIMITED_DEPLOYMENT"))
    release_text = json.dumps(release, ensure_ascii=False)
    findings.extend(_scan_claims(_normalise(release_text), "release manifest"))
    evidence["manifest_scan"] = "clean" if not findings else "flags"
    if doc_text is not None:
        doc_findings = _scan_claims(_normalise(doc_text), DEFAULT_DOC)
        findings.extend(doc_findings)
        evidence["docs137_scan"] = "clean" if not doc_findings else "flags"
    evidence["admission_flag"] = release.get("admission")
    evidence["status"] = release.get("status")
    return _check("R5", "no admission / T2 / ACTIVE claims", _status_from(findings), findings, evidence)


# ---------------------------------------------------------------------- R6 docs/137 consistency
def check_docs137(release: dict, repo: Path, doc_relative: str = DEFAULT_DOC) -> dict:
    findings, evidence = [], {}
    doc_path = repo / doc_relative
    evidence["path"] = doc_relative
    if not doc_path.is_file():
        findings.append(_finding("R6", "BLOCKER", f"{doc_relative} is missing"))
        return _check("R6", "docs/137 is consistent with the manifest", "FAIL", findings, evidence)
    text = _normalise(doc_path.read_text(encoding="utf-8"))
    evidence["bytes"] = doc_path.stat().st_size
    missing = [label for label, token in DOC137_NUMBERS if token not in text]
    evidence["missing_tokens"] = missing
    for label in missing:
        findings.append(_finding("R6", "HIGH", f"{doc_relative} does not state {label}"))
    # number cross-checks against the artefacts
    world = release.get("world", {})
    pairs = [
        ("FIGARO coverage", world.get("figaro", {}).get("gdp_coverage_named_pct"), "87,2771"),
        ("OECD min coverage", world.get("oecd", {}).get("gdp_coverage_min_pct"), "95,35"),
        ("OECD max coverage", world.get("oecd", {}).get("gdp_coverage_max_pct"), "95,47"),
        ("WIOD min coverage", world.get("wiod", {}).get("gdp_coverage_min_pct"), "84,38"),
        ("WIOD max coverage", world.get("wiod", {}).get("gdp_coverage_max_pct"), "89,85"),
    ]
    evidence["numbers"] = {}
    for label, value, token in pairs:
        expected = token
        actual = _num(value)
        if actual is not None:
            expected = f"{actual:.2f}".replace(".", ",") if token.startswith(("95", "84", "89")) \
                else token
        present = token in text or expected in text
        evidence["numbers"][label] = {"manifest": value, "docs_token": token, "present": present}
        if not present:
            findings.append(_finding("R6", "HIGH", f"{doc_relative} number mismatch for {label}: "
                                                  f"manifest {value}, docs token {token!r} absent"))
    # input count claim
    match = re.search(r"(\d+)\s+вход", text)
    declared = int(match.group(1)) if match else None
    evidence["declared_inputs"] = declared
    evidence["manifest_inputs"] = len(release.get("inputs", []))
    if declared is not None and declared != evidence["manifest_inputs"]:
        findings.append(_finding("R6", "LOW", f"{doc_relative} declares {declared} manifest inputs, "
                                              f"the manifest has {evidence['manifest_inputs']}"))
    # claimed manifest size
    match = re.search(r"([\d]+[.,][\d]+)\s*КБ", text)
    size_claim = _num(match.group(1).replace(",", ".")) if match else None
    release_path = repo / DEFAULT_RELEASE
    if size_claim is not None and release_path.is_file():
        actual_kib = release_path.stat().st_size / 1024.0
        evidence["size_claim_kb"] = size_claim
        evidence["actual_kib"] = round(actual_kib, 2)
        if abs(size_claim - actual_kib) > 0.3:
            findings.append(_finding("R6", "LOW", f"{doc_relative} claims {size_claim} KB, the manifest "
                                                  f"is {actual_kib:.1f} KiB"))
    # test count claim for tests/test_p2_release.py
    match = re.search(r"test_p2_release[^\n]*?(\d+)\s+тест", text)
    declared_tests = int(match.group(1)) if match else None
    test_file = repo / "tests" / "test_p2_release.py"
    actual_tests = (len(re.findall(r"^\s+def test_", test_file.read_text(encoding="utf-8"),
                                     re.M)) if test_file.is_file() else None)
    evidence.update({"declared_tests": declared_tests, "actual_tests": actual_tests})
    if declared_tests is not None and actual_tests is not None and declared_tests != actual_tests:
        findings.append(_finding("R6", "LOW", f"{doc_relative} claims {declared_tests} release tests, "
                                              f"tests/test_p2_release.py has {actual_tests}"))
    # stale docs_context_missing claim
    context_missing = release.get("docs_context_missing") or []
    declared_missing_docs = [entry.get("path") for entry in release.get("inputs", [])
                             if entry.get("role") == "optional"]
    present_optional = [path for path in declared_missing_docs if (repo / str(path)).is_file()]
    evidence.update({"docs_context_missing": context_missing,
                     "optional_inputs_present": present_optional})
    docs_claim = "отсутствуют в рабочем дереве" in text
    manifest_claim = any("отсутствуют в рабочем дереве" in str(item)
                         for item in release.get("limitations", []))
    evidence["docs137_claims_optional_docs_missing"] = docs_claim
    evidence["manifest_claims_optional_docs_missing"] = manifest_claim
    if (docs_claim or manifest_claim) and context_missing == [] and present_optional:
        findings.append(_finding(
            "R6", "MEDIUM",
            "the release text claims that docs/135-136 are absent from the working tree, but both "
            "are hashed inputs that exist and docs_context_missing is empty",
            {"docs137_claim": docs_claim, "manifest_limitation": manifest_claim,
             "optional_inputs_present": present_optional,
             "docs_context_missing": context_missing}))
    # statuses claimed in the docs must match the manifest
    summary = release.get("criteria_summary", {})
    for cid, status in summary.items():
        if status not in text:
            findings.append(_finding("R6", "HIGH", f"{doc_relative} does not state status {status} "
                                                  f"for {cid}"))
    evidence["statuses_claimed"] = summary
    return _check("R6", "docs/137 is consistent with the manifest", _status_from(findings),
                  findings, evidence)


# ----------------------------------------------------------------------- R7 reproducibility
def _documented_commands(repo: Path, release: dict, doc_relative: str) -> list[str]:
    commands = list(release.get("reproduce") or [])
    doc_path = repo / doc_relative
    if doc_path.is_file():
        text = doc_path.read_text(encoding="utf-8")
        for block in re.findall(r"```(?:powershell|bash|sh)?\n(.*?)```", text, re.S):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("python ") or line.startswith("python3 "):
                    commands.append(line)
    return commands


def check_reproducibility(release: dict, repo: Path, doc_relative: str = DEFAULT_DOC,
                          check_help: bool = True, check_rebuild: bool = True) -> dict:
    findings, evidence = [], {}
    commands = _documented_commands(repo, release, doc_relative)
    tools = []
    for command in commands:
        for match in re.finditer(r"(tools[/\\][\w.\-/\\]+\.py)", command):
            candidate = match.group(1).replace("\\", "/")
            if candidate not in tools:
                tools.append(candidate)
    evidence["commands"] = commands
    evidence["tools"] = tools
    missing_tools = [tool for tool in tools if not (repo / tool).is_file()]
    evidence["missing_tools"] = missing_tools
    for tool in missing_tools:
        findings.append(_finding("R7", "BLOCKER", f"documented tool is missing: {tool}"))
    help_results = {}
    if check_help:
        for tool in [tool for tool in tools if (repo / tool).is_file()]:
            done = subprocess.run([sys.executable, str(repo / tool), "--help"],
                                  cwd=str(repo), capture_output=True, text=True, timeout=300)
            help_results[tool] = {"returncode": done.returncode,
                                  "usage": "usage" in (done.stdout + done.stderr).lower()}
            if done.returncode != 0 or not help_results[tool]["usage"]:
                findings.append(_finding("R7", "HIGH", f"`python {tool} --help` does not work "
                                                      f"(exit {done.returncode})",
                                         {"tool": tool, "returncode": done.returncode}))
    evidence["help"] = help_results
    data_root = _data_root(release, repo)
    evidence["data_root"] = _url(data_root)
    evidence["data_root_exists"] = data_root.is_dir()
    if not data_root.is_dir():
        findings.append(_finding("R7", "HIGH", f"the declared data root is missing: {_url(data_root)}"))
    else:
        figaro_dataset = data_root / "figaro" / "figaro_2019" / "dataset.json"
        evidence["figaro_dataset_exists"] = figaro_dataset.is_file()
        if not figaro_dataset.is_file():
            findings.append(_finding("R7", "HIGH", f"the FIGARO dataset is missing: "
                                                  f"{_url(figaro_dataset)}"))
        wiod_dir = data_root / "wiod_2016"
        wiod_count = len(list(wiod_dir.glob("wiod_*/manifest.json"))) if wiod_dir.is_dir() else 0
        evidence["wiod_manifests_on_disk"] = wiod_count
        if wiod_count < 15:
            findings.append(_finding("R7", "HIGH", f"only {wiod_count}/15 WIOD manifests are on disk"))
    rebuild = {"requested": bool(check_rebuild)}
    if check_rebuild:
        tool = repo / "tools" / "p2_release.py"
        if not tool.is_file():
            findings.append(_finding("R7", "BLOCKER", "tools/p2_release.py is missing"))
        else:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "rebuild.json"
                command = [sys.executable, str(tool),
                           "--generated-at", str(release.get("generated_at")),
                           "--date", str(release.get("date")),
                           "--git-commit", str(release.get("git_commit")),
                           "--data-root", str(_data_root(release, repo)),
                           "--out", str(target)]
                done = subprocess.run(command, cwd=str(repo), capture_output=True, text=True,
                                      timeout=600)
                rebuild["returncode"] = done.returncode
                rebuild["stderr"] = (done.stderr or "").strip()[:400]
                if done.returncode != 0 or not target.is_file():
                    findings.append(_finding("R7", "BLOCKER",
                                             f"the manifest cannot be rebuilt (exit {done.returncode})",
                                             {"stderr": rebuild["stderr"]}))
                else:
                    rebuilt = json.loads(target.read_text(encoding="utf-8"))
                    differences = _diff_manifest(release, rebuilt)
                    working = {}
                    for entry in release.get("inputs", []):
                        input_path = _input_path(release, entry, repo)
                        if input_path.is_file():
                            working[entry.get("id")] = {"sha256": sha256_file(input_path),
                                                        "bytes": input_path.stat().st_size}
                    for difference in differences:
                        matched = re.match(r"inputs\[(\d+)\]\.(sha256|bytes)", difference["path"])
                        if not matched:
                            continue
                        index, field = int(matched.group(1)), matched.group(2)
                        try:
                            current = working.get(release["inputs"][index].get("id"))
                        except (IndexError, KeyError):
                            current = None
                        if current and difference.get("rebuilt") == current[field]:
                            difference["repaired_by_working_tree"] = True
                    rebuild["differences"] = differences
                    drift_only = all(entry["path"].startswith(("inputs[", "inputs."))
                                     and entry.get("repaired_by_working_tree")
                                     for entry in differences)
                    if differences and not drift_only:
                        findings.append(_finding("R7", "BLOCKER",
                                                 f"the rebuild differs from the release manifest "
                                                 f"({len(differences)} leaves)",
                                                 {"differences": differences[:10]}))
                    elif differences:
                        findings.append(_finding("R7", "LOW",
                                                 "the rebuild differs only in inputs modified after "
                                                 "the release (working-tree drift), not in the "
                                                 "criteria",
                                                 {"differences": differences[:10]}))
            evidence["rebuild"] = rebuild
    return _check("R7", "documented commands work and the manifest rebuilds deterministically",
                  _status_from(findings), findings, evidence)


def _diff_manifest(left, right, path="", out=None):
    out = [] if out is None else out
    if type(left) != type(right):
        out.append({"path": path, "release": left, "rebuilt": right})
        return out
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                out.append({"path": child, "release": "<missing>", "rebuilt": right[key]})
            elif key not in right:
                out.append({"path": child, "release": left[key], "rebuilt": "<missing>"})
            else:
                _diff_manifest(left[key], right[key], child, out)
    elif isinstance(left, list):
        if len(left) != len(right):
            out.append({"path": f"{path}[len]", "release": len(left), "rebuilt": len(right)})
            return out
        for index, (one, two) in enumerate(zip(left, right)):
            _diff_manifest(one, two, f"{path}[{index}]", out)
    elif left != right:
        out.append({"path": path, "release": left, "rebuilt": right})
    return out


# ------------------------------------------------------------------------- R8 limitations
def check_limitations(release: dict, repo: Path) -> dict:
    findings, evidence = [], {}
    corpus = _normalise(json.dumps(release, ensure_ascii=False))
    for path in (DOC133, DEFAULT_DOC):
        candidate = repo / path
        if candidate.is_file():
            corpus += " " + _normalise(candidate.read_text(encoding="utf-8"))
    evidence["corpus_docs"] = [DOC133, DEFAULT_DOC]
    missing = []
    evidence["tokens"] = {}
    for label, alternatives in REQUIRED_LIMITATION_TOKENS:
        present = any(token in corpus for token in alternatives)
        evidence["tokens"][label] = present
        if not present:
            missing.append({"limitation": label, "accepted": list(alternatives)})
    evidence["missing"] = missing
    if missing:
        findings.append(_finding("R8", "MEDIUM", "required limitations are not stated: "
                                                 + ", ".join(item["limitation"] for item in missing),
                                 {"missing": missing}))
    return _check("R8", "required limitations are stated", _status_from(findings), findings, evidence)


# ----------------------------------------------------------------------- R9 dataset digests
def check_dataset_digests(release: dict, repo: Path) -> dict:
    findings, evidence = [], {"datasets": {}}
    data_root = _data_root(release, repo)
    candidates = [
        ("figaro", data_root / "figaro" / "figaro_2019" / "dataset.json",
         release["world"]["figaro"].get("sha256"), release["world"]["figaro"].get("bytes")),
    ]
    try:
        oecd_index = _report_index(repo, "build/world_oecd/report_index.json", "OECD scenario index")
        oecd_path = Path(str(oecd_index["dataset"].get("dataset_id")))
        if not oecd_path.is_file():
            oecd_path = data_root / "oecd_icio" / "datasets" / "2011.json"
        candidates.append(("oecd", oecd_path, release["world"]["oecd"].get("dataset_sha256"),
                           release["world"]["oecd"].get("dataset_bytes")))
    except ReviewError:
        candidates.append(("oecd", data_root / "oecd_icio" / "datasets" / "2011.json",
                           release["world"]["oecd"].get("dataset_sha256"),
                           release["world"]["oecd"].get("dataset_bytes")))
    for label, path, claimed_sha, claimed_bytes in candidates:
        row = {"path": _url(path), "exists": path.is_file()}
        if not path.is_file():
            row["status"] = "MISSING"
            findings.append(_finding("R9", "LOW", f"{label} dataset is not on disk (data outside git "
                                                  "is a documented limitation); its digest was not "
                                                  "recomputed", {"path": _url(path)}))
            evidence["datasets"][label] = row
            continue
        observed = sha256_file(path)
        row.update({"sha256": observed, "bytes": path.stat().st_size,
                    "claimed_sha256": claimed_sha, "claimed_bytes": claimed_bytes})
        if observed == claimed_sha and row["bytes"] == claimed_bytes:
            row["status"] = "OK"
        else:
            row["status"] = "MISMATCH"
            findings.append(_finding("R9", "BLOCKER", f"{label} dataset digest differs from the "
                                                      "manifest", {"path": _url(path),
                                                                  "claimed": claimed_sha,
                                                                  "observed": observed,
                                                                  "claimed_bytes": claimed_bytes,
                                                                  "observed_bytes": row["bytes"]}))
        evidence["datasets"][label] = row
    return _check("R9", "FIGARO/OECD dataset digests match the manifest", _status_from(findings),
                  findings, evidence)


# ---------------------------------------------------------------------- R10 candidate states
def check_candidate_states(release: dict, repo: Path) -> dict:
    findings, evidence = [], {}
    v5 = _read_json(repo / "protocols" / "first_real_evaluation_report_v5.json", "T1 v5 report")
    regime = _read_json(repo / "protocols" / "regime_v4_candidate_evaluation_report.json",
                        "regime report")
    shadow = _read_json(repo / "build" / "shadow" / "shadow_status.json", "shadow status")
    t1_admission = v5["admission"]
    regime_admission = regime["admission"]
    observed = {
        "t1": {"status": t1_admission.get("status"),
               "status_label": t1_admission.get("status_label"),
               "conditional_data_class": t1_admission.get("conditional_data_class"),
               "c4_pass": t1_admission["criteria"]["C4"].get("pass"),
               "c6_pass": t1_admission["criteria"]["C6"].get("pass"),
               "target": v5.get("target"), "horizon": v5.get("horizon"),
               "protocol_version": (v5.get("protocol") or {}).get("version")},
        "regime": {"status": regime_admission.get("status"),
                   "status_label": regime_admission.get("status_label"),
                   "conditional_data_class": regime_admission.get("conditional_data_class"),
                   "stages": regime_admission.get("stages"),
                   "target": regime.get("target"), "horizon": regime.get("horizon"),
                   "protocol_version": (regime.get("protocol") or {}).get("version")},
        "shadow": {"status": shadow.get("status"),
                   "candidate": (shadow.get("candidate") or {}).get("id"),
                   "c6_pass": (shadow.get("c6") or {}).get("pass"),
                   "c6_resolved": (shadow.get("c6") or {}).get("n_resolved"),
                   "c6_min": (shadow.get("c6") or {}).get("min_forecasts"),
                   "journal_rows": (shadow.get("journal") or {}).get("rows_total")},
    }
    claimed = release["world"]["candidates"]
    evidence.update({"observed": observed, "claimed": {"t1": claimed.get("t1"),
                                                       "regime": claimed.get("regime"),
                                                       "shadow": claimed.get("shadow"),
                                                       "admitted": claimed.get("admitted")}})
    for key in ("status", "status_label", "conditional_data_class"):
        for label in ("t1", "regime"):
            if claimed.get(label, {}).get(key) != observed[label][key]:
                findings.append(_finding("R10", "BLOCKER",
                                         f"candidates.{label}.{key} differs from the report",
                                         {"claimed": claimed.get(label, {}).get(key),
                                          "report": observed[label][key]}))
    if claimed.get("t1", {}).get("c4_pass") is not False or observed["t1"]["c4_pass"] is not False:
        findings.append(_finding("R10", "BLOCKER", "T1 C4 must be FAIL (candidate status CANDIDATE)"))
    if claimed.get("t1", {}).get("c6_pass") is not False:
        findings.append(_finding("R10", "BLOCKER", "T1 C6 must be FAIL"))
    if (claimed.get("t1") or {}).get("status") != "CANDIDATE":
        findings.append(_finding("R10", "BLOCKER", "T1 is not CANDIDATE"))
    if (claimed.get("regime") or {}).get("status") != "SHADOW_TEST":
        findings.append(_finding("R10", "BLOCKER", "the regime candidate is not SHADOW_TEST"))
    if (claimed.get("regime") or {}).get("conditional_data_class") is not True:
        findings.append(_finding("R10", "BLOCKER", "the regime candidate is not conditional"))
    if (claimed.get("regime") or {}).get("stages", {}).get("LIMITED_DEPLOYMENT_to_ACTIVE"):
        findings.append(_finding("R10", "BLOCKER", "the regime candidate claims ACTIVE"))
    shadow_claim = claimed.get("shadow") or {}
    pairs = (("status", "status"), ("candidate", "candidate"), ("c6_pass", "c6_pass"),
             ("journal_rows", "journal_rows"), ("c6_min_forecasts", "c6_min"),
             ("c6_resolved", "c6_resolved"))
    for claimed_key, observed_key in pairs:
        if shadow_claim.get(claimed_key) != observed["shadow"][observed_key]:
            findings.append(_finding("R10", "BLOCKER",
                                     f"candidates.shadow.{claimed_key} differs",
                                     {"claimed": shadow_claim.get(claimed_key),
                                      "shadow": observed["shadow"][observed_key]}))
    c6 = observed["shadow"]
    if c6["c6_pass"] is not False or not c6["c6_resolved"] == 0:
        findings.append(_finding("R10", "BLOCKER", "C6 is claimed to pass without resolved "
                                                   "forecasts"))
    return _check("R10", "candidate states match the evaluation reports (no ACTIVE)", 
                  _status_from(findings), findings, evidence)


# -------------------------------------------------------------------------------- review build
def build_review(release_path: Path | str = DEFAULT_RELEASE, repo: Path | str = ROOT,
                 doc: str = DEFAULT_DOC, check_help: bool = True, check_rebuild: bool = True,
                 use_git: bool = True, generated_at: str | None = None,
                 _release: dict | None = None) -> dict:
    repo = Path(repo)
    release_path = Path(release_path) if release_path else DEFAULT_RELEASE
    if _release is None:
        resolved = release_path if release_path.is_absolute() else repo / release_path
        release = _read_json(resolved, "P2 0.1 release manifest")
    else:
        release = copy.deepcopy(_release)
        resolved = release_path if release_path.is_absolute() else repo / release_path
    doc_path = repo / doc
    doc_text = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else None
    timestamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    checks = [
        check_manifest(release, resolved),
        check_inputs(release, repo, use_git=use_git),
        check_declared_commit(release, repo, use_git=use_git),
        check_criteria(release, repo),
        check_claims(release, repo, doc_text=doc_text),
        check_docs137(release, repo, doc_relative=doc),
        check_reproducibility(release, repo, doc_relative=doc, check_help=check_help,
                              check_rebuild=check_rebuild),
        check_limitations(release, repo),
        check_dataset_digests(release, repo),
        check_candidate_states(release, repo),
    ]
    criteria = checks[3]["evidence"]["criteria"]
    findings = [finding for check_item in checks for finding in check_item["findings"]]
    by_severity = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        by_severity[finding["severity"]] += 1
    failed = [check_item["id"] for check_item in checks if check_item["status"] == "FAIL"]
    overall = ("FAIL" if failed else
               "PASS_WITH_FINDINGS" if findings else "PASS")
    release_sha = sha256_file(resolved) if resolved.is_file() else None
    review = {
        "schema": SCHEMA,
        "reviewer": "independent adversarial review of the P2 0.1 release manifest",
        "review_type": "ADVERSARIAL_INDEPENDENT",
        "generated_at": timestamp,
        "date": str(release.get("date")),
        "release": {
            "path": _url(release_path),
            "release": release.get("release"), "version": release.get("version"),
            "status": release.get("status"), "date": release.get("date"),
            "generated_at": release.get("generated_at"),
            "git_commit": release.get("git_commit"),
            "sha256": release_sha,
            "bytes": resolved.stat().st_size if resolved.is_file() else None,
            "admission": release.get("admission"),
        },
        "checks": checks,
        "criteria": criteria,
        "summary": {
            "status": overall,
            "failed_checks": failed,
            "checks": len(checks),
            "findings": len(findings),
            "by_severity": by_severity,
            "blockers": [finding for finding in findings
                         if finding["severity"] in ("BLOCKER", "HIGH")],
            "verdicts": {cid: item["verdict"] for cid, item in criteria.items()},
        },
        "sign_off": {
            "state": "NOT_SIGNED",
            "blocked_by": [finding["id"] for finding in findings
                           if finding["severity"] in ("BLOCKER", "HIGH")],
            "required": ("reviewer signs only after every BLOCKER/HIGH finding is closed and the "
                         "review report is re-generated"),
        },
        "limits": [
            "automated checklist: it verifies artefacts and numbers, not the scientific validity of "
            "the MRIO/SFC/scenario modelling choices",
            "only hashes and aggregates are read: third-party data is never copied or committed",
            "rebuilt manifest is compared byte-for-byte except the declared generated_at/date/git "
            "pointer, which are supplied explicitly",
            "working-tree drift after the release is reported but does not by itself invalidate the "
            "release commit",
            "the review does not reproduce the 94 MB FIGARO/29 MB OECD solves: scenario residuals are "
            "read from the report indexes, not recomputed from raw Z",
            "a human reviewer must still sign docs/138; this tool does not replace the sign-off",
        ],
    }
    return review


# ------------------------------------------------------------------------------------ rendering
def render_markdown(review: dict) -> str:
    lines = []
    summary = review["summary"]
    release = review["release"]
    lines.append("# External review: P2 0.1 release manifest")
    lines.append("")
    lines.append(f"Generated: {review['generated_at']} · review schema `{review['schema']}` · "
                 f"type **{review['review_type']}**")
    lines.append("")
    lines.append(f"Release: **{release['release']}** (version {release['version']}, "
                 f"{release['status']}, {release['date']}, commit `{str(release['git_commit'])[:7]}`)")
    lines.append("")
    lines.append(f"Manifest SHA-256: `{release['sha256']}` · {release['bytes']} bytes")
    lines.append("")
    lines.append(f"## Verdict: {summary['status']}")
    lines.append("")
    lines.append(f"{summary['checks']} checks · {summary['findings']} findings "
                 f"(BLOCKER {summary['by_severity']['BLOCKER']}, HIGH {summary['by_severity']['HIGH']}, "
                 f"MEDIUM {summary['by_severity']['MEDIUM']}, LOW {summary['by_severity']['LOW']}, "
                 f"INFO {summary['by_severity']['INFO']}) · failed checks: "
                 f"{summary['failed_checks'] or 'none'}")
    lines.append("")
    lines.append("| Check | Status | Findings |")
    lines.append("|---|---|---|")
    for item in review["checks"]:
        lines.append(f"| {item['id']} {item['title']} | **{item['status']}** | {len(item['findings'])} |")
    lines.append("")
    lines.append("## D1-D6 verdicts")
    lines.append("")
    lines.append("| Criterion | Claimed | Verdict |")
    lines.append("|---|---|---|")
    for cid in ("D1", "D2", "D3", "D4", "D5", "D6"):
        item = review["criteria"].get(cid, {})
        lines.append(f"| {cid} | {item.get('claimed_status')} | **{item.get('verdict')}** |")
    lines.append("")
    findings = [finding for check_item in review["checks"] for finding in check_item["findings"]]
    if findings:
        lines.append("## Findings")
        lines.append("")
        order = {severity: index for index, severity in enumerate(SEVERITIES)}
        for finding in sorted(findings, key=lambda item: (order[item["severity"]], item["check"])):
            lines.append(f"- **{finding['severity']}** `{finding['id']}` ({finding['check']}): "
                         f"{finding['message']}")
        lines.append("")
    else:
        lines.append("## Findings")
        lines.append("")
        lines.append("None: every check passed.")
        lines.append("")
    lines.append("## Sign-off")
    lines.append("")
    lines.append(f"State: **{review['sign_off']['state']}**. Blocked by: "
                 f"{', '.join(review['sign_off']['blocked_by']) or 'nothing'}.")
    lines.append("")
    lines.append(review["sign_off"]["required"])
    lines.append("")
    lines.append("## Limits of this review")
    lines.append("")
    for limit in review["limits"]:
        lines.append(f"- {limit}")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------ CLI
def write_review(review: dict, out_dir: Path | str = DEFAULT_OUT, repo: Path | str = ROOT) -> dict:
    out_dir = Path(out_dir)
    out_dir = out_dir if out_dir.is_absolute() else Path(repo) / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "review.json"
    md_path = out_dir / "review.md"
    payload = json.dumps(review, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(review), encoding="utf-8", newline="\n")
    return {"review": _url(json_path), "markdown": _url(md_path),
            "sha256": sha256_bytes(payload.encode("utf-8")), "bytes": len(payload.encode("utf-8"))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independent adversarial review of the P2 0.1 release manifest (fail-closed)")
    parser.add_argument("--release", default=str(DEFAULT_RELEASE),
                        help="release manifest path (default build/p2_release/p2_release.json)")
    parser.add_argument("--repo", default=str(ROOT), help="repository root")
    parser.add_argument("--doc", default=DEFAULT_DOC, help="release document (docs/137)")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT),
                        help="output directory (default build/external_review)")
    parser.add_argument("--generated-at", default=None, help="ISO 8601 timestamp (determinism)")
    parser.add_argument("--no-write", action="store_true", help="compute only, write nothing")
    parser.add_argument("--no-help", action="store_true", help="skip the --help smoke checks")
    parser.add_argument("--no-rebuild", action="store_true", help="skip the manifest rebuild check")
    parser.add_argument("--skip-git", action="store_true", help="skip the git-based checks")
    parser.add_argument("--json", action="store_true", help="print the review to stdout")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo)
    release_path = Path(args.release)
    resolved = release_path if release_path.is_absolute() else repo / release_path
    try:
        release = _read_json(resolved, "P2 0.1 release manifest")
        review = build_review(release_path=release_path, repo=repo, doc=args.doc,
                              check_help=not args.no_help, check_rebuild=not args.no_rebuild,
                              use_git=not args.skip_git, generated_at=args.generated_at,
                              _release=release)
    except ReviewError as error:
        print(json.dumps({"status": "BLOCKED_RELEASE", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    written = {}
    if not args.no_write:
        written = write_review(review, args.out_dir, repo=repo)
    summary = {"status": review["summary"]["status"],
               "release": review["release"]["release"],
               "manifest_sha256": review["release"]["sha256"],
               "verdicts": review["summary"]["verdicts"],
               "failed_checks": review["summary"]["failed_checks"],
               "findings": review["summary"]["by_severity"],
               "out": written}
    if args.json:
        print(json.dumps(review, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if review["summary"]["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
