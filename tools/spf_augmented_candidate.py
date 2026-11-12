#!/usr/bin/env python3
"""SPF-augmented T2 candidate: a 5-member equal-weight ensemble in the harness format.

What this file is
-----------------
A *candidate definition plus its forecast function* for the T2 target (real GDP growth, protocol v4)
in the exact dict format produced by ``amfe.ensemble.ensemble.candidate_forecast`` and consumed by
``tools/run_real_evaluation.py`` / the admission harness.  Two candidate variants are defined, in
registration order (control first, then the SPF-augmented version):

``ENSEMBLE_T2_NOT_SPF``  control: four internal R3 members (AO, AR_BIC, BVAR_MINNESOTA,
                         OWN_NOWCAST_BRIDGE) with the declared ``ENSEMBLE_EQUAL`` scheme; this is the
                         "current ensemble without SPF" reference used in docs/95.
``ENSEMBLE_T2_SPF``      the same scheme with the external ``SPF_GDP_MEDIAN`` member added **when the
                         SPF median is public at the origin** per ``src/amfe/ensemble/spf_calendar.py``
                         (official news-release date; docs/86).  Weights are recomputed (equal
                         weights over the available member set), not fixed; when the median is not
                         public the candidate falls back to the four internal members and the
                         forecast is identical to the control.

The tool does **not** register the candidates: registration is a Lead action (protocol
``model_class_rules.registry_access``); this file provides ``CANDIDATE_IDS``,
``CANDIDATE_DESCRIPTION``, ``candidate_definition()``, ``candidates_document()`` and the
``candidate_forecast()`` entry point the harness dispatcher needs.

Why T2 only
-----------
The SPF publishes real GDP growth medians (``drgdp2`` for the survey quarter, docs/86 §2-3) and has
no monthly year-over-year CPI (T1) or NBER recession probability (T3).  The T2 mapping is
h=0 -> drgdp2, h=1 -> drgdp3, h=2 -> drgdp4, h=4 -> drgdp6; h=3 is not on the protocol grid.

Availability and fallback
-------------------------
The median becomes public at the **news release**, not at the response deadline.  ``SPF`` enters the
ensemble only if: (a) the survey is real time (>= 1990Q3); (b) the origin is the month-end of the
middle month of the survey quarter (the frozen alignment rule in ``amfe.ensemble.sources``); and
(c) the origin is on/after the official release date.  Under protocol v4 the h=1/2/4 origins are
quarter ends, so the frozen rule never admits the SPF member there; the SPF variant therefore falls
back to the control at those horizons.  This is recorded explicitly (``spf_available``,
``spf_fallback``, ``spf_reason``) rather than hidden.

Licence
-------
SPF medians are third-party data (licence register ``PHILADELPHIA_FED``, research use): they are read
only from the git-ignored ``data/raw/spf/spf_median.json``, are never written to the repository, and
the forecast output keeps the attribution.  ``publish_external_values`` stays False, so no SPF value
appears in the returned dict.

Usage
-----
    python tools/spf_augmented_candidate.py --definition      # registration fragments (JSON)
    python tools/spf_augmented_candidate.py --check           # spec + SPF availability plan
    python tools/spf_augmented_candidate.py --origin 2019-05-31 --horizon 0 \
        --candidate ENSEMBLE_T2_SPF                          # forecast summary (derived stats only)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.ensemble.ensemble import ENSEMBLE_EQUAL, ensemble_forecast  # noqa: E402
from amfe.ensemble.history import default_member_ids  # noqa: E402
from amfe.ensemble.sources import SPF_ATTRIBUTION, spf_forecast  # noqa: E402
from amfe.forecast.data import as_date, month_end_of  # noqa: E402

TOOL = "tools/spf_augmented_candidate.py"
TARGET = "T2_GDP_GROWTH"
HORIZONS = (0, 1, 2, 4)
#: The SPF code per protocol T2 horizon (docs/86 §2-3, official SPF documentation §3).
SPF_CODE_BY_HORIZON = {0: "drgdp2", 1: "drgdp3", 2: "drgdp4", 4: "drgdp6"}
EXTERNAL_MEMBER = "SPF_GDP_MEDIAN"

CANDIDATE_ID_NOT_SPF = "ENSEMBLE_T2_NOT_SPF"
CANDIDATE_ID_SPF = "ENSEMBLE_T2_SPF"
#: Registration order: the control (no SPF) first, then the SPF-augmented candidate.
CANDIDATE_IDS = (CANDIDATE_ID_NOT_SPF, CANDIDATE_ID_SPF)
CANDIDATE_DESCRIPTION = {
    CANDIDATE_ID_NOT_SPF: ("control: equal-weight ensemble of the four internal T2 R3 members "
                           "(no external forecast)"),
    CANDIDATE_ID_SPF: ("SPF-augmented T2 ensemble: the same equal-weight scheme with the "
                       "Philadelphia Fed SPF median as a fifth member when the median is public at "
                       "the origin; falls back to the four internal members otherwise"),
}
WEIGHT_SCHEME = ("ENSEMBLE_EQUAL: equal weights over the members available at the origin, "
                 "renormalised when a member is missing (the SPF member enters/leaves the set; the "
                 "weights are recomputed, not fixed)")
AVAILABILITY_RULE = (
    "SPF median is public at the origin iff the survey quarter is real time (>= 1990Q3), the origin "
    "is the month-end of the middle month of the survey quarter (frozen protocol alignment) and the "
    "origin is on/after the official news-release date from src/amfe/ensemble/spf_calendar.py "
    "(docs/86 §5). Under protocol v4 the h=1/2/4 origins are quarter ends, so SPF enters only at h=0 "
    "middle-month origins; elsewhere the candidate falls back to the control."
)
FALLBACK_RULE = ("if the SPF median is not public (or its values are not present in the git-ignored "
                 "data directory), the SPF candidate uses exactly the control member set and reports "
                 "spf_available=false, spf_fallback=true with the explicit reason")
LIMITATIONS = [
    "the SPF mapping is declared by the project (docs/86) on top of the official SPF documentation; "
    "INFCPI1YR/INFCPI10YR are not T1 forecasts and are not used by this candidate",
    "the SPF values are the latest file revision, not a point-in-time vintage; the release-date rule "
    "fixes availability but errata/revisions are not audited (docs/86 §8)",
    "the SPF member's predictive distribution is a project-derived statistic (empirical historical "
    "SPF errors centred on the median), not a Philadelphia Fed density",
    "the frozen alignment rule admits SPF only at h=0 middle-month origins; the candidate is "
    "identical to the control at h=1/2/4 until the external-member alignment is extended (a frozen "
    "src/amfe/ensemble change, Lead decision)",
    "the candidate is diagnostic until registered by the Lead and scored through the admission "
    "harness; docs/95 sensitivity is not an admission result",
]


class CandidateError(ValueError):
    """Invalid candidate id, target, horizon or origin."""


# ----------------------------------------------------------------------------------------------
# Definitions (registration fragments)
# ----------------------------------------------------------------------------------------------
def internal_members(horizon: int = 0) -> list:
    """The four internal R3 members applicable to T2 at the horizon (frozen R3 specification)."""
    return list(default_member_ids(TARGET, int(horizon), include_external=False))


def candidate_variant(candidate_id: str) -> str:
    if candidate_id == CANDIDATE_ID_NOT_SPF:
        return "NOT_SPF"
    if candidate_id == CANDIDATE_ID_SPF:
        return "SPF"
    raise CandidateError(f"unknown candidate {candidate_id!r}; known: {', '.join(CANDIDATE_IDS)}")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def candidate_definition(candidate_id: str) -> dict:
    """Registration-ready fragment for one candidate (the Lead merges it into the document)."""
    variant = candidate_variant(candidate_id)
    members = internal_members(0)
    if variant == "SPF":
        members = members + [EXTERNAL_MEMBER]
    return {
        "id": candidate_id,
        "description": CANDIDATE_DESCRIPTION[candidate_id],
        "role": "control" if variant == "NOT_SPF" else "candidate",
        "code_file": TOOL,
        "code_sha256": _file_sha256(ROOT / TOOL),
        "targets": [TARGET],
        "horizons_quarters": list(HORIZONS),
        "members": members,
        "weight_scheme": WEIGHT_SCHEME,
        "availability_rule": AVAILABILITY_RULE,
        "fallback": FALLBACK_RULE,
        "spf_attribution": SPF_ATTRIBUTION,
        "spf_mapping": {str(h): SPF_CODE_BY_HORIZON[h] for h in HORIZONS},
        "registration_status": "NOT_REGISTERED",
        "note": ("defined by tools/spf_augmented_candidate.py; the Lead registers the candidate in "
                 "protocols/candidates_*.json and the harness dispatcher before any evaluation"),
    }


def candidates_document() -> dict:
    """Registration proposal document; ``registered=false`` - this tool never registers anything."""
    return {
        "document_id": "AMFE-CANDIDATES",
        "proposal_from": TOOL,
        "registered": False,
        "note": ("proposal only: the Lead merges the candidates into protocols/candidates_*.json "
                 "(order: control first, then the SPF-augmented candidate) and adds the dispatcher "
                 "branch; no candidate is registered by this file"),
        "candidates": [candidate_definition(candidate_id) for candidate_id in CANDIDATE_IDS],
    }


def spec() -> dict:
    """Machine-readable specification of both variants (used by docs/100 and the tests)."""
    return {
        "tool": TOOL,
        "candidate_ids": list(CANDIDATE_IDS),
        "registration_order": "control (NOT_SPF) first, SPF-augmented second",
        "target": TARGET,
        "target_definition": "T2_GDP_GROWTH = 400*ln(GDP_q/GDP_{q-1}) (annualised q/q real GDP growth)",
        "horizons_quarters": list(HORIZONS),
        "base_ensemble": ENSEMBLE_EQUAL,
        "internal_members": internal_members(0),
        "external_member": EXTERNAL_MEMBER,
        "weight_scheme": WEIGHT_SCHEME,
        "availability_rule": AVAILABILITY_RULE,
        "fallback": FALLBACK_RULE,
        "spf_mapping": {str(h): SPF_CODE_BY_HORIZON[h] for h in HORIZONS},
        "spf_attribution": SPF_ATTRIBUTION,
        "registration_status": "NOT_REGISTERED",
        "limitations": list(LIMITATIONS),
    }


# ----------------------------------------------------------------------------------------------
# Forecast API (harness-compatible)
# ----------------------------------------------------------------------------------------------
def _validate(target: str, horizon: int) -> int:
    if target != TARGET:
        raise CandidateError(
            f"the SPF-augmented candidate is T2-only: target {target!r} is not supported "
            f"(SPF has no monthly y/y CPI for T1 and no NBER probability for T3)")
    horizon_i = int(horizon)
    if horizon_i not in HORIZONS:
        raise CandidateError(
            f"unsupported T2 horizon {horizon_i}: the protocol grid is {list(HORIZONS)} "
            "(h=3/drgdp5 exists in the SPF file but is not on the protocol grid)")
    return horizon_i


def member_plan(candidate_id: str, target: str, origin, horizon: int, *, data_root=None) -> dict:
    """Which members enter the candidate at the origin, and the SPF availability verdict.

    Pure availability/membership logic (no member forecasts), so tests can exercise the fallback
    without a local data archive.
    """
    variants = candidate_variant(candidate_id)
    horizon_i = _validate(target, horizon)
    origin_d = month_end_of(as_date(origin))
    plan = {
        "candidate_id": candidate_id,
        "variant": variants,
        "target": TARGET,
        "horizon": horizon_i,
        "origin": origin_d.isoformat(),
        "internal_members": internal_members(horizon_i),
        "spf_available": False,
        "spf_reason": "NOT_APPLICABLE_NOT_SPF",
        "spf_fallback": False,
        "member_ids": internal_members(horizon_i),
    }
    if variants == "SPF":
        verdict = spf_forecast(TARGET, origin_d, horizon_i, data_root=data_root)
        plan["spf_available"] = bool(verdict.available)
        plan["spf_reason"] = None if verdict.available else str(verdict.reason)
        plan["spf_fallback"] = not verdict.available
        if verdict.available:
            plan["member_ids"] = internal_members(horizon_i) + [EXTERNAL_MEMBER]
    return plan


def candidate_forecast(candidate_id: str, target: str, origin, horizon: int,
                       series_id: str | None = None, mode: str = "auto",
                       data_root=None) -> dict:
    """Harness-compatible forecast dict for one candidate at one origin.

    Signature-compatible with ``amfe.candidates.models.candidate_forecast`` and
    ``amfe.ensemble.ensemble.candidate_forecast``; the returned dict keeps the base ensemble format
    and adds the candidate/variant/SPF-availability fields.
    """
    plan = member_plan(candidate_id, target, origin, horizon, data_root=data_root)
    result = ensemble_forecast(
        ENSEMBLE_EQUAL, TARGET, origin, horizon,
        member_ids=plan["member_ids"], series_id=series_id, mode=mode, data_root=data_root,
    )
    result["candidate_id"] = candidate_id
    result["candidate_variant"] = plan["variant"]
    result["base_ensemble_id"] = result.get("ensemble_id", ENSEMBLE_EQUAL)
    result["spf_available"] = plan["spf_available"]
    result["spf_reason"] = plan["spf_reason"]
    result["spf_fallback"] = plan["spf_fallback"]
    result["spf_horizon_code"] = SPF_CODE_BY_HORIZON[plan["horizon"]]
    result["definition"] = {"tool": TOOL, "target": TARGET,
                            "registration_status": "NOT_REGISTERED"}
    return result


# ----------------------------------------------------------------------------------------------
# Availability plan (fast check) and CLI
# ----------------------------------------------------------------------------------------------
def availability_plan(*, data_root=None, window: str = "W1") -> dict:
    """Counts of origins where the SPF member would enter, per horizon (no member forecasts)."""
    import windows  # tools/windows.py

    protocol = json.loads((ROOT / "protocols" / "evaluation_protocol_v4.json").read_text(encoding="utf-8"))
    out = {"target": TARGET, "window": window, "horizons": {}}
    for horizon in HORIZONS:
        origins = [row["origin"] for row in windows.origins_for(protocol, TARGET, horizon, window)]
        available = 0
        reasons: dict[str, int] = {}
        first = last = None
        for origin in origins:
            plan = member_plan(CANDIDATE_ID_SPF, TARGET, origin, horizon, data_root=data_root)
            if plan["spf_available"]:
                available += 1
                first = first or plan["origin"]
                last = plan["origin"]
            else:
                reason = str(plan["spf_reason"])
                reasons[reason] = reasons.get(reason, 0) + 1
        out["horizons"][str(horizon)] = {
            "origins": len(origins), "spf_available": available,
            "spf_fraction": round(available / len(origins), 6) if origins else None,
            "first_available": first, "last_available": last,
            "not_available_reasons": dict(sorted(reasons.items())),
        }
    return out


def _forecast_summary(result: dict) -> dict:
    """Aggregate-only summary of one forecast (no SPF values, no member points)."""
    quantiles = result.get("quantiles")
    digest = None
    if quantiles is not None:
        import numpy as np
        digest = hashlib.sha256(np.asarray(quantiles, dtype=float).tobytes()).hexdigest()
    return {
        "candidate_id": result["candidate_id"],
        "variant": result["candidate_variant"],
        "target": result["target"],
        "origin": result["origin"],
        "horizon": result["horizon"],
        "data_class": result["data_class"],
        "point_rounded": round(float(result["point"]), 6),
        "quantiles_n": None if quantiles is None else len(quantiles),
        "quantiles_sha256": digest,
        "members": list(result.get("member_weights") or {}),
        "member_weights": {member: round(float(w), 6)
                           for member, w in (result.get("member_weights") or {}).items()},
        "spf_available": result["spf_available"],
        "spf_reason": result["spf_reason"],
        "spf_fallback": result["spf_fallback"],
        "spf_horizon_code": result["spf_horizon_code"],
        "registration_status": result["definition"]["registration_status"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SPF-augmented T2 candidate definition and forecasts")
    parser.add_argument("--definition", action="store_true",
                        help="print the registration fragments (JSON) and exit")
    parser.add_argument("--spec", action="store_true", help="print the machine-readable spec and exit")
    parser.add_argument("--check", action="store_true",
                        help="print the spec and the SPF availability plan for W1 (no member forecasts)")
    parser.add_argument("--origin", default=None, help="month-end origin (YYYY-MM-DD) for a forecast")
    parser.add_argument("--horizon", type=int, default=0, choices=list(HORIZONS))
    parser.add_argument("--candidate", default=CANDIDATE_ID_SPF, choices=list(CANDIDATE_IDS))
    parser.add_argument("--data-root", default=None, help="data/raw root (default: repository data/raw)")
    parser.add_argument("--window", default="W1", choices=["W1", "W2", "W3"])
    args = parser.parse_args(argv)
    if args.definition:
        print(json.dumps(candidates_document(), ensure_ascii=False, indent=1))
        return 0
    if args.spec:
        print(json.dumps(spec(), ensure_ascii=False, indent=1))
        return 0
    if args.check:
        payload = {"spec": spec(), "availability": availability_plan(data_root=args.data_root,
                                                                     window=args.window)}
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0
    if args.origin is None:
        parser.error("one of --definition/--spec/--check/--origin is required")
    result = candidate_forecast(args.candidate, TARGET, args.origin, args.horizon,
                                data_root=args.data_root)
    print(json.dumps(_forecast_summary(result), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
