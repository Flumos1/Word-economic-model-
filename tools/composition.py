#!/usr/bin/env python3
"""Primary-comparator composition of the frozen evaluation protocol v4.

Protocol v4, primary_comparator_rule and comparator_minimum:
  * point losses  -> EQUAL_WEIGHT_POINT, members that provide a point forecast;
  * density losses -> EQUAL_WEIGHT_DENSITY, members that provide a predictive distribution
    (protocol forecast_product.baseline_distributions maps RW/AO/AR_BIC error quantiles to a
    distribution, so only a member explicitly flagged point_only is excluded);
  * event losses (T3) -> EQUAL_WEIGHT_DENSITY = equal-weight average of event probabilities.

Mechanical rule (never a researcher choice):
  member  <=>  comparator_member is true
           and scored is not false
           and the target is in applies_to
           and every input listed for that target is OPEN in the licence register on the
               evaluation date (the register resolves the status of the latest entry dated
               on or before the evaluation date; a change is a new dated entry, never an edit).

The composition is written to protocols/comparator_composition_<date>.json as canonical JSON
(sorted keys, UTF-8, LF, two-space indent) and its SHA-256 to the matching .sha256 file, both
before any candidate is evaluated (r3_specification.comparator_procedure.composition_record).

The per-origin REAL_TIME condition of primary_comparator_rule is *not* part of this document:
it is a property of the data gate at each origin (protocol data_depth, r3 data_manifest) and is
applied when the historical forecast sample is built; this document records the register state
on the evaluation date.  The document states that explicitly in its notes.

Usage:
    python tools/composition.py                      # writes the document for 2026-11-11
    python tools/composition.py --date 2026-11-11    # same
    python tools/composition.py --check              # rebuilds and verifies file + hash
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
REGISTER = ROOT / "protocols" / "licence_register.json"
R3 = ROOT / "protocols" / "r3_specification.json"
OUT_DIR = ROOT / "protocols"

FIRST_EVALUATION_DATE = "2026-11-11"
MIN_MEMBERS = 2

# Which loss serves which kind of target (protocol losses.*).
LOSS_SETS = {
    "point_and_distribution": {"point": "RMSE", "density": "CRPS"},
    "event_probability": {"event": ["Brier", "log_score"]},
}


class CompositionError(RuntimeError):
    pass


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def lf_sha256(path: Path) -> str:
    """SHA-256 of the file with CRLF normalised to LF (the repository convention)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def canonical_text(document: dict) -> str:
    """Canonical JSON: sorted keys, UTF-8, LF line endings, two-space indent, final newline."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def out_name(evaluation_date: str) -> tuple[Path, Path]:
    return (
        OUT_DIR / f"comparator_composition_{evaluation_date}.json",
        OUT_DIR / f"comparator_composition_{evaluation_date}.sha256",
    )


def resolve_statuses(register: dict, evaluation_date: str) -> dict[str, str]:
    """Register status per source id on the evaluation date.

    The register is an append-only journal: a status change is a new dated entry.  The status
    in force is the one of the latest entry whose date is on or before the evaluation date.
    """
    day = date.fromisoformat(evaluation_date)
    resolved: dict[str, tuple[str, str]] = {}
    for entry in register["entries"]:
        entry_date = entry["date"]
        if date.fromisoformat(entry_date) > day:
            continue
        # later entries win; ties are rejected because the journal must stay unambiguous
        if entry["id"] in resolved and resolved[entry["id"]][0] == entry_date:
            raise CompositionError(f"two register entries for {entry['id']} dated {entry_date}")
        resolved[entry["id"]] = (entry_date, entry["status"])
    return {key: value[1] for key, value in resolved.items()}


def target_horizons(protocol: dict, target: dict) -> list[int]:
    if "horizons_months" in target:
        return list(target["horizons_months"])
    return list(target["horizons_quarters"])


def member_horizons(baseline: dict, horizons: list[int]) -> list[int]:
    """Horizons at which a baseline is specified; an unrestricted baseline serves all."""
    for key in ("horizons_months", "horizons_quarters"):
        if key in baseline:
            return [h for h in horizons if h in baseline[key]]
    return list(horizons)


def applicable_kind(protocol: dict, target_id: str) -> str:
    for target in protocol["targets"]:
        if target["id"] == target_id:
            return target["kind"]
    raise CompositionError(f"unknown target {target_id}")


def build_composition(evaluation_date: str = FIRST_EVALUATION_DATE) -> dict:
    protocol = _load(PROTOCOL)
    register = _load(REGISTER)
    statuses = resolve_statuses(register, evaluation_date)
    minimum = int(protocol["comparator_minimum"]["min_members"])
    if minimum != MIN_MEMBERS:
        raise CompositionError(f"protocol minimum changed to {minimum}, tool expects {MIN_MEMBERS}")

    targets_block: dict[str, dict] = {}
    for target in protocol["targets"]:
        target_id = target["id"]
        kind = target["kind"]
        horizons = target_horizons(protocol, target)
        loss_sets = LOSS_SETS[kind]
        excluded: list[dict] = []

        eligible: list[dict] = []
        for baseline in protocol["baselines"]:
            if target_id not in baseline.get("applies_to", []):
                continue
            if not baseline.get("comparator_member", False):
                excluded.append({"id": baseline["id"], "reason": "comparator_member is false"})
                continue
            if baseline.get("scored", True) is False:
                excluded.append({"id": baseline["id"], "reason": "scored is false"})
                continue
            inputs = baseline.get("inputs_by_target", {}).get(target_id, [])
            if not inputs:
                excluded.append({"id": baseline["id"], "reason": "no inputs_by_target entry"})
                continue
            input_status = {source: statuses.get(source, "NOT_IN_REGISTER") for source in inputs}
            bad = {source: status for source, status in input_status.items() if status != "OPEN"}
            if bad:
                excluded.append({"id": baseline["id"], "reason": "input not OPEN on the evaluation date",
                                 "inputs": bad})
                continue
            hset = member_horizons(baseline, horizons)
            if not hset:
                excluded.append({"id": baseline["id"], "reason": "no horizon overlaps the target"})
                continue
            eligible.append({
                "id": baseline["id"],
                "inputs": input_status,
                "horizons": hset,
                "provides_distribution": not baseline.get("point_only", False),
                "note": baseline.get("note", ""),
            })

        loss_block: dict[str, dict] = {}
        all_covered = True
        if kind == "point_and_distribution":
            for loss_set, loss_name in loss_sets.items():
                members = [m for m in eligible
                           if loss_set != "density" or m["provides_distribution"]]
                by_horizon = {}
                for h in horizons:
                    ids = sorted(m["id"] for m in members if h in m["horizons"])
                    by_horizon[str(h)] = {
                        "members": ids,
                        "count": len(ids),
                        "minimum_members": minimum,
                        "minimum_met": len(ids) >= minimum,
                    }
                    all_covered = all_covered and len(ids) >= minimum
                loss_block[loss_name] = {
                    "loss_set": loss_set,
                    "members": sorted(members, key=lambda m: m["id"]),
                    "by_horizon": by_horizon,
                    "minimum_met": all(v["minimum_met"] for v in by_horizon.values()),
                }
        else:
            members = sorted(eligible, key=lambda m: m["id"])
            by_horizon = {}
            for h in horizons:
                ids = sorted(m["id"] for m in members if h in m["horizons"])
                by_horizon[str(h)] = {
                    "members": ids, "count": len(ids),
                    "minimum_members": minimum, "minimum_met": len(ids) >= minimum,
                }
                all_covered = all_covered and len(ids) >= minimum
            for loss_name in loss_sets["event"]:
                loss_block[loss_name] = {
                    "loss_set": "event",
                    "members": members,
                    "by_horizon": by_horizon,
                    "minimum_met": all(v["minimum_met"] for v in by_horizon.values()),
                }

        targets_block[target_id] = {
            "kind": kind,
            "horizons": horizons,
            "losses": loss_block,
            "excluded": sorted(excluded, key=lambda e: e["id"]),
            "eligible_for_admission": all_covered,
            "all_horizons_have_minimum": all_covered,
        }

    document = {
        "document_id": "AMFE-COMPARATOR-COMPOSITION",
        "protocol": {
            "file": "protocols/evaluation_protocol_v4.json",
            "version": protocol["version"],
            "sha256": lf_sha256(PROTOCOL),
        },
        "r3_specification": {
            "file": "protocols/r3_specification.json",
            "version": _load(R3)["version"],
            "sha256": lf_sha256(R3),
        },
        "licence_register": {
            "file": "protocols/licence_register.json",
            "sha256": lf_sha256(REGISTER),
        },
        "evaluation_date": evaluation_date,
        "rule": protocol["primary_comparator_rule"],
        "minimum_members": minimum,
        "minimum_rule": protocol["comparator_minimum"]["rule"],
        "licence_inputs_by_target": protocol["comparator_minimum"]["licence_inputs_by_target"],
        "primary_comparator": {
            "point": "EQUAL_WEIGHT_POINT",
            "density": "EQUAL_WEIGHT_DENSITY",
            "event": "EQUAL_WEIGHT_DENSITY",
        },
        "distribution_rule": protocol["forecast_product"]["baseline_distributions"],
        "targets": targets_block,
        "notes": [
            "Mechanical function of the frozen protocol and the licence register; never a researcher choice.",
            "The register status is the status of the latest dated entry on or before the evaluation date.",
            "The REAL_TIME condition of primary_comparator_rule applies per origin through the data gate "
            "and is not recorded here; this document fixes the member list, not the data availability of an origin.",
            "A register change after this document is recorded applies only to candidates evaluated later; "
            "the sensitivity comparator of fixed composition is reported separately and never used for admission.",
        ],
    }
    return document


def write_document(evaluation_date: str = FIRST_EVALUATION_DATE) -> tuple[Path, Path, str]:
    document = build_composition(evaluation_date)
    out_json, out_pin = out_name(evaluation_date)
    text = canonical_text(document)
    out_json.write_bytes(text.encode("utf-8"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out_pin.write_bytes(
        f"{digest}  {out_json.name} (sha256 of the LF-normalised file)\n".encode("utf-8"))
    return out_json, out_pin, digest


def check_document(evaluation_date: str = FIRST_EVALUATION_DATE) -> tuple[bool, str, str]:
    """Rebuild in memory and verify the written file and its pinned digest."""
    out_json, out_pin = out_name(evaluation_date)
    if not out_json.exists():
        raise CompositionError(f"{out_json} does not exist")
    if not out_pin.exists():
        raise CompositionError(f"{out_pin} does not exist")
    expected = canonical_text(build_composition(evaluation_date))
    actual = out_json.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    pinned = out_pin.read_text(encoding="utf-8").split()[0]
    if actual != expected:
        return False, "document differs from the rebuilt canonical composition", digest
    if digest != pinned:
        return False, "pinned SHA-256 differs from the rebuilt digest", digest
    return True, "document and SHA-256 match the rebuilt canonical composition", digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Primary-comparator composition (protocol v4)")
    parser.add_argument("--date", default=FIRST_EVALUATION_DATE,
                        help="evaluation date (YYYY-MM-DD), default: the first evaluation date")
    parser.add_argument("--check", action="store_true",
                        help="rebuild in memory and verify the recorded document and its SHA-256")
    args = parser.parse_args(argv)
    date.fromisoformat(args.date)  # validate
    if args.check:
        ok, message, digest = check_document(args.date)
        print(json.dumps({"check": "PASS" if ok else "FAIL", "message": message,
                          "sha256": digest}, ensure_ascii=False))
        return 0 if ok else 1
    path, pin, digest = write_document(args.date)
    print(json.dumps({"written": str(path.relative_to(ROOT)).replace("\\", "/"),
                      "sha256_file": str(pin.relative_to(ROOT)).replace("\\", "/"),
                      "sha256": digest}, ensure_ascii=False))
    document = build_composition(args.date)
    for target_id, block in document["targets"].items():
        summary = {loss: {h: v["count"] for h, v in data["by_horizon"].items()}
                   for loss, data in block["losses"].items()}
        print(f"{target_id}: {json.dumps(summary, ensure_ascii=False)} "
              f"eligible={block['eligible_for_admission']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompositionError as error:  # pragma: no cover - CLI guard
        print(f"COMPOSITION ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
