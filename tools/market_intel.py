#!/usr/bin/env python3
"""Validate the AMFE market-intelligence org and generate role task briefs.

The network is **not** used: the tool only reads local JSON/Markdown and never invents facts. It is
fail-closed (any structural problem is an error, exit 2) and deterministic (no clock, no random, no
environment-dependent ordering). Nothing is written unless ``--out`` is given, and then only the
brief file the caller asked for.

The network runs in **OSINT_ONLY** mode: there are no subscriptions, only open source classes
``S1``-``S3`` are allowed, ``S4`` (licensed dataset) is ``DEFERRED`` and ``S5`` (closed source) is
forbidden. ``--check`` enforces that mode and refuses an active licensed-data role.

Gated sources (``G0`` public, ``G1`` registration, ``G2`` licensed, ``G3`` NDA data room, ``G4``
restricted) are recognised, never bypassed: ``--check`` validates the gated policy, the access-request
template, the access journal and the absence of credentials; ``--access-request`` drafts a lawful
request for a person/Lead to approve. Nothing here obtains access.

Commands::

    python tools/market_intel.py --check                 # exit 0 / 2, prints the issues
    python tools/market_intel.py --org                   # human-readable org chart + process
    python tools/market_intel.py --json                  # machine summary (amfe.market_intel.check.v1)
    python tools/market_intel.py --mode osint --check    # explicit mode (osint is the default)
    python tools/market_intel.py --role upstream --question "..." --account TATNEFT
    python tools/market_intel.py --role upstream --question "..." --out .tmp/brief.md
    python tools/market_intel.py --access-request --source "Price DB" --gated-class G2 \
        --purpose "..." --legal-basis "..." --out .tmp/access_request.md

Exit codes: 0 = ok, 2 = invalid input/failed check (fail-closed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORG = ROOT / "agents" / "market_intelligence" / "org.json"
DEFAULT_ACCOUNTS = ROOT / "marketing" / "target_accounts.json"

ORG_SCHEMA = "amfe.market_intelligence.org.v1"
CHECK_SCHEMA = "amfe.market_intel.check.v1"
ACCESS_REQUEST_SCHEMA = "amfe.market_intel.access_request.v1"

MODES = ("osint",)
DEFAULT_MODE = "osint"
ORG_MODE = "OSINT_ONLY"
OPEN_SOURCE_CLASSES = ("S1", "S2", "S3")
DEFERRED_SOURCE_CLASSES = ("S4",)
FORBIDDEN_SOURCE_CLASSES = ("S5",)
LICENSED_ROLE = "licensed-data"
LICENSED_ROLE_STATUSES = ("DEFERRED", "DISABLED")
OSINT_ROLE = "osint-scout"
ROLE_STATUSES = ("ACTIVE", "DEFERRED", "DISABLED")
ACTIVE_STATUS = "ACTIVE"

PROCESS_ORDER = ("question", "source_plan", "collect", "grade", "analysis", "case", "qa", "kb")
PROCESS_STATES = ("QUESTION", "SOURCE_PLAN", "COLLECT", "GRADE", "ANALYSIS", "CASE", "QA", "KB")
LAYERS = ("lead", "segment", "collection", "control", "output")
GRADES = ("A", "B", "C")
STATUSES = ("VERIFIED", "PARTIAL", "HYPOTHESIS", "NOT_FETCHED")
STATUS_RANK = {"NOT_FETCHED": 0, "HYPOTHESIS": 1, "PARTIAL": 2, "VERIFIED": 3}

REQUIRED_ROLE_FIELDS = (
    "id", "title", "title_ru", "layer", "mandate", "segments", "accounts", "questions", "inputs",
    "allowed", "forbidden", "outputs", "evidence_ceiling", "review_role", "handoff_to", "prompt",
    "memory",
)
REQUIRED_PROMPT_SECTIONS = (
    "## Мандат",
    "## Входы",
    "## Можно",
    "## Нельзя",
    "## Формат отчёта",
    "## Критерий приёмки",
    "## Передача",
)
REQUIRED_BOUNDARIES = (
    "lawful_sources_only",
    "open_sources_only",
    "allowed_sources",
    "licensed_databases",
    "osint",
    "gated_access",
    "no_paywall_bypass",
    "no_closed_databases",
    "no_robots_terms_violations",
    "no_personal_data",
    "personal_data",
    "integrity",
    "sanctions",
    "network_in_env",
    "code",
    "publication",
    "no_fabrication",
)
PLAYBOOKS = ("lawful_osint.md", "source_hierarchy.md", "evidence_grading.md", "red_lines.md",
             "numeric_facts.md", "gated_sources.md", "access_request.md")
SOURCE_HIERARCHY_MARKERS = ("S1", "S2", "S3", "S4", "S5", "OSINT_ONLY", "paywall", "DEFERRED")
LAWFUL_OSINT_MARKERS = ("paywall", "robots", "S1–S3", "OSINT_ONLY", "персональные данные")
RED_LINES_MARKERS = ("RL-19", "RL-20", "RL-21", "RL-22", "OSINT_ONLY", "paywall", "robots")

# --- gated sources (lawful access, never bypass) ------------------------------------------------
GATED_SCHEMA = "amfe.market_intelligence.gated.v1"
GATED_PLAYBOOK = "gated_sources.md"
ACCESS_REQUEST_PLAYBOOK = "access_request.md"
GATED_CLASSES = ("G0", "G1", "G2", "G3", "G4")
GATED_ACCESS = ("OPEN", "OPEN_REGISTRATION", "LICENCE_REQUIRED", "NDA", "NO_ACCESS")
GATED_STATUSES = ("AUTHORIZED", "REQUEST_PENDING", "NOT_AUTHORIZED")
GATED_POLICY = "RECOGNIZE_AND_REQUEST"
GATED_COLLECT_NOW = {"G0": True, "G1": True, "G2": False, "G3": False, "G4": False}
GATED_REQUEST_REQUIRED = {"G0": False, "G1": False, "G2": True, "G3": True, "G4": False}
GATED_SOURCE_CLASSES = {
    "G0": ("S1", "S2", "S3"),
    "G1": ("S1", "S2", "S3"),
    "G2": ("S4",),
    "G3": ("S4",),
    "G4": ("S5",),
}
GATED_JOURNAL_FIELDS = ("request_id", "role", "source", "gated_class", "legal_basis", "purpose",
                        "requested_at", "decision", "decided_at", "expires_at", "owner")
GATED_NO_BYPASS_TOPICS = ("paywall", "авторизац", "утёкш", "персональн", "nda", "ключ")
GATED_SKILLS = (
    "public_records",
    "court_registries_public",
    "procurement_portals",
    "sanctions_screening",
    "beneficial_ownership_public",
    "foia_public_records_requests",
    "licensed_apis",
    "data_rooms_nda",
    "deep_research",
    "source_triangulation",
    "quote_extraction",
    "access_request",
)
FORBIDDEN_SKILLS = ("paywall_bypass", "captcha_bypass", "credential_sharing", "leaked_database",
                    "scraping_limits_bypass")
GATED_MARKERS = {
    GATED_PLAYBOOK: ("G0", "G1", "G2", "G3", "G4", "NDA", "FOIA", "API key", "NOT_AUTHORIZED",
                     "журнал", "обход"),
    ACCESS_REQUEST_PLAYBOOK: ("правовое основание", "цель", "срок", "не копиру", "Lead",
                              "NOT_AUTHORIZED", "кто"),
}
#: Imperative recipes for obtaining access without a right; never allowed in the gated docs.
BYPASS_RECIPE_MARKERS = ("как обойти", "how to bypass", "bypass the paywall", "обойти защиту",
                         "обход через кэш", "кэш платной статьи", "подобрать пароль",
                         "украсть ключ", "зеркало платной", "купить базу", "слить базу")
#: Credential shapes; matched values are never echoed back into logs.
CREDENTIAL_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("api_key_literal", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("generic_secret", re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd|bearer)"
        r"\b\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9_\-\.]{11,})")),
)

ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?:\+\d[\d\s()-]{7,}\d)|\b\d{3}[\s-]\d{3}[\s-]\d{2}[\s-]\d{2}\b")
ADMISSION_RE = re.compile(r"\b(admitted|допущенн\w*|допуск\s+модели)\b", re.IGNORECASE)

MIN_ROLES = 9          # 1 lead + 8
MAX_ROLES = 11         # 1 lead + 10
MAX_LEAD_CEILING = "PARTIAL"


class OrgError(ValueError):
    """The org file cannot be read/parsed at all (fail-closed input)."""


def _issue(code: str, field: str, message: str, role: str | None = None) -> dict:
    return {"code": code, "field": field, "message": message, "role": role}


def load_org(path: Path = DEFAULT_ORG) -> dict:
    """Load the org JSON; raises :class:`OrgError` when missing or not an object."""
    path = Path(path)
    if not path.exists():
        raise OrgError(f"file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise OrgError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise OrgError(f"{path}: top-level value must be an object")
    return data


def load_accounts(path: Path = DEFAULT_ACCOUNTS) -> dict[str, dict]:
    """Load the internal target-account card keyed by id (empty when unavailable)."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    return {row["id"]: row for row in data if isinstance(row, dict) and row.get("id")}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def find_credentials(text: str) -> list[str]:
    """Return credential *kinds* found in ``text``; the values are never returned or logged."""
    return sorted({kind for kind, pattern in CREDENTIAL_PATTERNS if pattern.search(text)})


def find_bypass_recipes(text: str) -> list[str]:
    """Return imperative bypass recipes found in ``text`` (lowercased markers, fail-closed)."""
    low = text.lower()
    return [marker for marker in BYPASS_RECIPE_MARKERS if marker in low]


def _require_list(role: dict, field: str, issues: list[dict]) -> list:
    value = role.get(field)
    if not isinstance(value, list):
        issues.append(_issue("field_type", field, f"{field} must be a list", role.get("id")))
        return []
    return value


def check_roles(org: dict, org_dir: Path, accounts: dict[str, dict],
                accounts_error: str | None) -> list[dict]:
    """Structural checks of the role roster, prompts, coverage and independence."""
    issues: list[dict] = []
    roles = org.get("roles")
    if not isinstance(roles, list) or not roles:
        return [_issue("roles", "roles", "roles must be a non-empty list")]

    total = len(roles)
    if not MIN_ROLES <= total <= MAX_ROLES:
        issues.append(_issue("role_count", "roles",
                             f"expected {MIN_ROLES}..{MAX_ROLES} roles (1 lead + 8..10), got {total}"))

    ids: list[str] = []
    prompts: dict[str, str] = {}
    lead_ids: list[str] = []
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            issues.append(_issue("role_type", f"roles[{index}]", "role must be an object"))
            continue
        role_id = _text(role.get("id"))
        label = role_id or f"roles[{index}]"
        for field in REQUIRED_ROLE_FIELDS:
            if field not in role:
                issues.append(_issue("required", field, f"missing field {field}", label))
        if role_id and not ROLE_ID_RE.match(role_id):
            issues.append(_issue("id_format", "id", "id must be kebab-case ^[a-z][a-z0-9-]{1,40}$", label))
        if role_id:
            if role_id in ids:
                issues.append(_issue("duplicate_id", "id", f"duplicate role id {role_id}", label))
            ids.append(role_id)

        for field in ("title", "title_ru", "mandate"):
            if not _text(role.get(field)):
                issues.append(_issue("empty", field, f"{field} must be a non-empty string", label))
        layer = _text(role.get("layer"))
        if layer and layer not in LAYERS:
            issues.append(_issue("layer", "layer", f"layer must be one of {list(LAYERS)}", label))
        status = _text(role.get("status"))
        if status and status not in ROLE_STATUSES:
            issues.append(_issue("role_status", "status",
                                 f"status must be one of {list(ROLE_STATUSES)}", label))
        if layer == "lead":
            lead_ids.append(role_id)

        ceiling = _text(role.get("evidence_ceiling"))
        if ceiling and ceiling not in STATUSES:
            issues.append(_issue("evidence_ceiling", "evidence_ceiling",
                                 f"evidence_ceiling must be one of {list(STATUSES)}", label))

        for field in ("questions", "inputs", "allowed", "forbidden", "outputs", "handoff_to"):
            values = _require_list(role, field, issues)
            if field != "handoff_to" and not values:
                issues.append(_issue("empty", field, f"{field} must not be empty", label))
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    issues.append(_issue("list_item", field,
                                         f"{field} items must be non-empty strings", label))
        _require_list(role, "segments", issues)
        _require_list(role, "accounts", issues)

        prompt = _text(role.get("prompt"))
        if not prompt:
            issues.append(_issue("empty", "prompt", "prompt path must be set", label))
        else:
            path = org_dir / prompt
            if prompt in prompts:
                issues.append(_issue("prompt_reuse", "prompt",
                                     f"prompt reused by {prompts[prompt]}", label))
            prompts[prompt] = role_id
            if not path.is_file():
                issues.append(_issue("prompt_missing", "prompt", f"prompt file not found: {prompt}", label))
            else:
                text = path.read_text(encoding="utf-8")
                for section in REQUIRED_PROMPT_SECTIONS:
                    if section not in text:
                        issues.append(_issue("prompt_section", "prompt",
                                             f"{prompt}: missing section {section!r}", label))
                if len(text) < 400:
                    issues.append(_issue("prompt_short", "prompt",
                                         f"{prompt}: too short to be a role charter", label))
                for kind in find_credentials(text):
                    issues.append(_issue("credential", "prompt",
                                         f"{prompt}: credential-like value ({kind}); "
                                         "keep secrets out of the repository", label))

        memory = _text(role.get("memory"))
        if not memory:
            issues.append(_issue("empty", "memory", "memory path must be set", label))
        elif not (memory.startswith("agents/memory/") and memory.endswith(".jsonl")):
            issues.append(_issue("memory_path", "memory",
                                 "memory must be agents/memory/<role>.jsonl", label))

    # OSINT_ONLY roster: the licensed collector is deferred (kept), the OSINT collector stays active.
    statuses = {_text(r.get("id")): (_text(r.get("status")) or ACTIVE_STATUS)
                for r in roles if isinstance(r, dict)}
    if LICENSED_ROLE not in statuses:
        issues.append(_issue("licensed_role_missing", "roles",
                             f"{LICENSED_ROLE} must stay in org.json (deferred, not deleted)"))
    elif statuses[LICENSED_ROLE] not in LICENSED_ROLE_STATUSES:
        issues.append(_issue("licensed_role_active", "status",
                             f"{LICENSED_ROLE} must be DEFERRED/DISABLED in {ORG_MODE}, "
                             f"got {statuses[LICENSED_ROLE]!r}", LICENSED_ROLE))
    if OSINT_ROLE not in statuses:
        issues.append(_issue("osint_role_missing", "roles", f"{OSINT_ROLE} is required"))
    elif statuses[OSINT_ROLE] != ACTIVE_STATUS:
        issues.append(_issue("osint_role_inactive", "status",
                             f"{OSINT_ROLE} must stay {ACTIVE_STATUS} in {ORG_MODE}", OSINT_ROLE))
    for role in roles:
        if not isinstance(role, dict) or _text(role.get("id")) == LICENSED_ROLE:
            continue
        for item in role.get("allowed") or []:
            if isinstance(item, str) and "лицензир" in item.lower() and "OSINT_ONLY" not in item:
                issues.append(_issue("licensed_allowed", "allowed",
                                     "licensed-data access must be marked DEFERRED/OSINT_ONLY",
                                     _text(role.get("id"))))

    lead = _text(org.get("lead"))
    if not lead:
        issues.append(_issue("lead_missing", "lead", "org.lead must name the Director role"))
    elif lead not in ids:
        issues.append(_issue("lead_unknown", "lead", f"lead {lead!r} is not a role id"))
    elif lead_ids != [lead]:
        issues.append(_issue("lead_count", "lead",
                             f"exactly one role must have layer 'lead' and it must be {lead!r}; "
                             f"got {lead_ids}"))

    team_limit = org.get("team_limit")
    if team_limit != total:
        issues.append(_issue("team_limit", "team_limit",
                             f"team_limit {team_limit!r} != number of roles {total}"))

    # Independence: review_role resolves to a role or a declared external reviewer, is not self,
    # and the chain terminates without cycles.
    external = {}
    for entry in org.get("external_reviewers") or []:
        if isinstance(entry, dict) and _text(entry.get("id")):
            external[_text(entry.get("id"))] = entry
            if not _text(entry.get("source")) or not _text(entry.get("reason")):
                issues.append(_issue("external_reviewer", "external_reviewers",
                                     "external reviewer needs id, source and reason"))
    by_id = {r.get("id"): r for r in roles if isinstance(r, dict)}
    for role in roles:
        if not isinstance(role, dict):
            continue
        role_id = _text(role.get("id"))
        reviewer = _text(role.get("review_role"))
        if not reviewer:
            issues.append(_issue("review_missing", "review_role", "review_role is required", role_id))
            continue
        if reviewer == role_id:
            issues.append(_issue("self_review", "review_role",
                                 "review_role cannot be the role itself", role_id))
            continue
        if reviewer in external:
            continue
        if reviewer not in by_id:
            issues.append(_issue("review_unknown", "review_role",
                                 f"review_role {reviewer!r} is not a role id or external reviewer",
                                 role_id))
            continue
        seen = [role_id]
        current = reviewer
        while current and current in by_id:
            if current in seen:
                issues.append(_issue("review_cycle", "review_role",
                                     f"review chain cycles: {seen} -> {current}", role_id))
                break
            seen.append(current)
            current = _text(by_id[current].get("review_role"))
            if len(seen) > 6:
                issues.append(_issue("review_cycle", "review_role",
                                     f"review chain too long from {role_id}", role_id))
                break

    # Gated sources: the OSINT collector recognises G0-G4 and requests access, it never bypasses.
    osint_role = by_id.get(OSINT_ROLE)
    if isinstance(osint_role, dict):
        skills = osint_role.get("skills")
        if not isinstance(skills, list) or not skills:
            issues.append(_issue("gated_skills", "skills",
                                 f"{OSINT_ROLE} must declare gated skills", OSINT_ROLE))
        else:
            missing = [skill for skill in GATED_SKILLS if skill not in skills]
            if missing:
                issues.append(_issue("gated_skills", "skills",
                                     f"{OSINT_ROLE} misses gated skills {missing}", OSINT_ROLE))
            banned = sorted(skill for skill in skills if skill in FORBIDDEN_SKILLS)
            if banned:
                issues.append(_issue("gated_forbidden_skill", "skills",
                                     f"{OSINT_ROLE} declares bypass skills {banned}", OSINT_ROLE))
        if tuple(osint_role.get("gated_classes") or ()) != GATED_CLASSES:
            issues.append(_issue("gated_class_decl", "gated_classes",
                                 f"{OSINT_ROLE} gated_classes must be {list(GATED_CLASSES)}",
                                 OSINT_ROLE))
        if _text(osint_role.get("gated_policy")) != GATED_POLICY:
            issues.append(_issue("gated_policy", "gated_policy",
                                 f"{OSINT_ROLE} gated_policy must be {GATED_POLICY!r}", OSINT_ROLE))
        if _text(osint_role.get("evidence_ceiling")) != "PARTIAL":
            issues.append(_issue("gated_ceiling_role", "evidence_ceiling",
                                 f"{OSINT_ROLE} evidence_ceiling must stay PARTIAL: gated access "
                                 "does not certify VERIFIED", OSINT_ROLE))
        substitution = osint_role.get("substitution")
        if not isinstance(substitution, dict):
            issues.append(_issue("substitution_block", "substitution",
                                 f"{OSINT_ROLE} must declare the licence-substitution policy",
                                 OSINT_ROLE))
        else:
            playbook = _text(substitution.get("playbook"))
            if not playbook or not (org_dir / playbook).is_file():
                issues.append(_issue("substitution_playbook", "substitution.playbook",
                                     f"substitution playbook not found: {playbook!r}", OSINT_ROLE))
            if not _text(substitution.get("rule")):
                issues.append(_issue("substitution_rule", "substitution.rule",
                                     "substitution rule text is required", OSINT_ROLE))
            required = substitution.get("required_fields") or []
            for field in ("substitution_for", "covers", "does_not_cover",
                          "frequency_change", "pit"):
                if field not in required:
                    issues.append(_issue("substitution_fields", "substitution.required_fields",
                                         f"{field} must be a required substitution field",
                                         OSINT_ROLE))
            if "licence_substitution" not in (osint_role.get("skills") or []):
                issues.append(_issue("substitution_skill", "skills",
                                     f"{OSINT_ROLE} must carry the licence_substitution skill",
                                     OSINT_ROLE))
        allowed_text = " ".join(item for item in (osint_role.get("allowed") or [])
                                if isinstance(item, str)).lower()
        if "access request" not in allowed_text and "запрос доступа" not in allowed_text:
            issues.append(_issue("gated_allowed", "allowed",
                                 f"{OSINT_ROLE} must be allowed to draft an access request",
                                 OSINT_ROLE))
        forbidden_text = " ".join(item for item in (osint_role.get("forbidden") or [])
                                  if isinstance(item, str)).lower()
        if "обход" not in forbidden_text:
            issues.append(_issue("gated_forbidden", "forbidden",
                                 f"{OSINT_ROLE} must explicitly forbid bypass", OSINT_ROLE))
        outputs_text = " ".join(item for item in (osint_role.get("outputs") or [])
                                if isinstance(item, str)).lower()
        for marker in ("access request", "журнал"):
            if marker not in outputs_text:
                issues.append(_issue("gated_output", "outputs",
                                     f"{OSINT_ROLE} outputs must include {marker!r}", OSINT_ROLE))

    # Who may certify VERIFIED: a control role, at least evidence-qa; lead stays at <= PARTIAL.
    signers = sorted(_text(r.get("id")) for r in roles
                     if isinstance(r, dict) and _text(r.get("evidence_ceiling")) == "VERIFIED")
    if "evidence-qa" not in signers:
        issues.append(_issue("qa_signer", "evidence_ceiling",
                             "evidence-qa must be allowed to sign VERIFIED"))
    for signer in signers:
        role = by_id.get(signer, {})
        if _text(role.get("layer")) not in ("control", "collection"):
            issues.append(_issue("signer_layer", "evidence_ceiling",
                                 f"{signer} certifies VERIFIED but is not a control/collection role",
                                 signer))
    lead_role = by_id.get(lead, {})
    if _text(lead_role.get("evidence_ceiling")) and \
            STATUS_RANK.get(_text(lead_role.get("evidence_ceiling")), 0) > STATUS_RANK[MAX_LEAD_CEILING]:
        issues.append(_issue("lead_ceiling", "evidence_ceiling",
                             f"lead evidence_ceiling must be <= {MAX_LEAD_CEILING}", lead))

    # Segment catalogue and coverage.
    catalog = org.get("segments")
    known: dict[str, dict] = {}
    if not isinstance(catalog, list) or not catalog:
        issues.append(_issue("segments", "segments", "segments catalogue must be a non-empty list"))
        catalog = []
    for entry in catalog:
        if not isinstance(entry, dict):
            issues.append(_issue("segment_type", "segments", "segment must be an object"))
            continue
        seg_id = _text(entry.get("id"))
        if not seg_id:
            issues.append(_issue("segment_id", "segments", "segment id is required"))
            continue
        known[seg_id] = entry
        if not isinstance(entry.get("covered_by"), list):
            issues.append(_issue("segment_covered_by", "segments",
                                 f"{seg_id}: covered_by must be a list"))
        if not isinstance(entry.get("in_portfolio"), bool):
            issues.append(_issue("segment_in_portfolio", "segments",
                                 f"{seg_id}: in_portfolio must be a boolean"))
        for covered in entry.get("covered_by") or []:
            if covered not in ids:
                issues.append(_issue("segment_role_unknown", "segments",
                                     f"{seg_id}: covered_by {covered!r} is not a role id"))
        contributors = entry.get("contributors", [])
        if not isinstance(contributors, list):
            issues.append(_issue("segment_contributors", "segments",
                                 f"{seg_id}: contributors must be a list"))
        else:
            for contributor in contributors:
                if contributor not in ids:
                    issues.append(_issue("segment_role_unknown", "segments",
                                         f"{seg_id}: contributor {contributor!r} is not a role id"))
                elif contributor in (entry.get("covered_by") or []):
                    issues.append(_issue("segment_contributor_primary", "segments",
                                         f"{seg_id}: {contributor} is both owner and contributor"))
    for role in roles:
        if not isinstance(role, dict) or not isinstance(role.get("segments"), list):
            continue
        for seg in role["segments"]:
            if seg not in known:
                issues.append(_issue("segment_unknown", "segments",
                                     f"role declares unknown segment {seg!r}", role.get("id")))
    for seg_id, entry in known.items():
        declared = sorted(r.get("id") for r in roles
                          if isinstance(r, dict) and seg_id in (r.get("segments") or []))
        declared = [item for item in declared if item]
        covered_by = [item for item in (entry.get("covered_by") or []) if item in ids]
        if entry.get("in_portfolio") and not covered_by:
            issues.append(_issue("segment_uncovered", "segments",
                                 f"portfolio segment {seg_id} has no covering role"))
        if sorted(covered_by) != sorted(declared):
            issues.append(_issue("segment_mismatch", "segments",
                                 f"{seg_id}: covered_by {sorted(covered_by)} != roles declaring it "
                                 f"{sorted(declared)}"))

    # Account coverage: every portfolio account owned exactly once; no invented ids.
    assigned: dict[str, str] = {}
    for role in roles:
        if not isinstance(role, dict):
            continue
        role_id = _text(role.get("id"))
        for account in role.get("accounts") or []:
            if not isinstance(account, str):
                issues.append(_issue("account_type", "accounts", "account id must be a string", role_id))
                continue
            if accounts and account not in accounts:
                issues.append(_issue("account_unknown", "accounts",
                                     f"{account!r} is not in the target-accounts file", role_id))
            if account in assigned:
                issues.append(_issue("account_duplicate", "accounts",
                                     f"{account} assigned to {assigned[account]} and {role_id}", role_id))
            else:
                assigned[account] = role_id
    if not accounts:
        issues.append(_issue("accounts_unavailable", "accounts",
                             f"target-accounts file unavailable: {accounts_error or 'not found'}"))
    else:
        missing = sorted(set(accounts) - set(assigned))
        extra = sorted(set(assigned) - set(accounts))
        if missing:
            issues.append(_issue("account_uncovered", "accounts",
                                 f"accounts without an owning role: {missing}"))
        if extra:
            issues.append(_issue("account_extra", "accounts",
                                 f"roles own accounts absent from the file: {extra}"))
        for account, role_id in sorted(assigned.items()):
            segment = (accounts.get(account) or {}).get("segment")
            if segment and segment not in known:
                issues.append(_issue("segment_missing", "segments",
                                     f"account {account} has segment {segment!r} not in the catalogue",
                                     role_id))
    return issues


def check_gated(org: dict, org_dir: Path) -> list[dict]:
    """Validate the G0-G4 policy, the access-request playbook and the no-credential rule."""
    issues: list[dict] = []
    block = org.get("gated_sources")
    if not isinstance(block, dict):
        return [_issue("gated_block", "gated_sources",
                       "gated_sources block is required: G0-G4 classification")]
    if _text(block.get("schema")) != GATED_SCHEMA:
        issues.append(_issue("gated_schema", "gated_sources.schema", f"schema must be {GATED_SCHEMA!r}"))
    for key in ("playbook", "access_request_template"):
        relative = _text(block.get(key))
        if not relative or not (org_dir / relative).is_file():
            issues.append(_issue("gated_playbook", f"gated_sources.{key}",
                                 f"{key} must point at an existing playbook, got {relative!r}"))
    if not _text(block.get("mode_note")):
        issues.append(_issue("gated_note", "gated_sources.mode_note", "mode_note is required"))

    classes = block.get("classes")
    if not isinstance(classes, list) or not classes:
        issues.append(_issue("gated_classes", "gated_sources.classes",
                             "classes must be a non-empty list"))
        classes = []
    ids = [_text(entry.get("id")) for entry in classes if isinstance(entry, dict)]
    if tuple(ids) != GATED_CLASSES:
        issues.append(_issue("gated_class_order", "gated_sources.classes",
                             f"classes must be exactly {list(GATED_CLASSES)}, got {ids}"))
    by_id = {entry.get("id"): entry for entry in classes if isinstance(entry, dict)}
    for class_id in GATED_CLASSES:
        entry = by_id.get(class_id)
        if not isinstance(entry, dict):
            issues.append(_issue("gated_class_missing", "gated_sources.classes",
                                 f"missing gated class {class_id}"))
            continue
        if not _text(entry.get("title")) or not _text(entry.get("title_ru")):
            issues.append(_issue("gated_class_title", f"gated_sources.{class_id}",
                                 "title and title_ru are required"))
        access = _text(entry.get("access"))
        if access not in GATED_ACCESS:
            issues.append(_issue("gated_access", f"gated_sources.{class_id}.access",
                                 f"access must be one of {list(GATED_ACCESS)}"))
        requirements = entry.get("requirements")
        if not isinstance(requirements, list) or not requirements or \
                any(not isinstance(item, str) or not item.strip() for item in requirements):
            issues.append(_issue("gated_requirements", f"gated_sources.{class_id}.requirements",
                                 "requirements must be a non-empty list of strings"))
        if not isinstance(entry.get("collect_now"), bool):
            issues.append(_issue("gated_collect", f"gated_sources.{class_id}.collect_now",
                                 "collect_now must be a boolean"))
        elif entry["collect_now"] is not GATED_COLLECT_NOW[class_id]:
            issues.append(_issue("gated_collect_policy", f"gated_sources.{class_id}.collect_now",
                                 f"{class_id} collect_now must be {GATED_COLLECT_NOW[class_id]} "
                                 f"in {ORG_MODE}"))
        if not isinstance(entry.get("request_required"), bool):
            issues.append(_issue("gated_request_flag", f"gated_sources.{class_id}.request_required",
                                 "request_required must be a boolean"))
        elif entry["request_required"] is not GATED_REQUEST_REQUIRED[class_id]:
            issues.append(_issue("gated_request_policy", f"gated_sources.{class_id}.request_required",
                                 f"{class_id} request_required must be "
                                 f"{GATED_REQUEST_REQUIRED[class_id]}"))
        if not _text(entry.get("request_path")):
            issues.append(_issue("gated_request_path", f"gated_sources.{class_id}.request_path",
                                 "request_path is required"))
        ceiling = _text(entry.get("evidence_ceiling"))
        if ceiling not in STATUSES:
            issues.append(_issue("gated_ceiling", f"gated_sources.{class_id}.evidence_ceiling",
                                 f"evidence_ceiling must be one of {list(STATUSES)}"))
        elif STATUS_RANK.get(ceiling, 0) > STATUS_RANK[MAX_LEAD_CEILING]:
            issues.append(_issue("gated_ceiling_policy", f"gated_sources.{class_id}.evidence_ceiling",
                                 f"gated evidence_ceiling must stay <= {MAX_LEAD_CEILING} in "
                                 f"{ORG_MODE}"))
        declared = entry.get("source_classes")
        if not isinstance(declared, list) or not declared:
            issues.append(_issue("gated_source_classes", f"gated_sources.{class_id}.source_classes",
                                 "source_classes must be a non-empty list"))
        else:
            allowed = GATED_SOURCE_CLASSES[class_id]
            extra = [item for item in declared if item not in allowed]
            if extra:
                issues.append(_issue("gated_source_classes_policy",
                                     f"gated_sources.{class_id}.source_classes",
                                     f"{class_id} may only map to {list(allowed)}, got {extra}"))

    statuses = block.get("statuses")
    if not isinstance(statuses, list) or tuple(statuses) != GATED_STATUSES:
        issues.append(_issue("gated_statuses", "gated_sources.statuses",
                             f"statuses must be {list(GATED_STATUSES)}"))
    titles = block.get("status_titles")
    if not isinstance(titles, dict) or any(not _text(titles.get(status)) for status in GATED_STATUSES):
        issues.append(_issue("gated_status_titles", "gated_sources.status_titles",
                             "status_titles must describe every status"))

    journal = block.get("journal")
    if not isinstance(journal, dict):
        issues.append(_issue("gated_journal", "gated_sources.journal",
                             "journal block is required (access journal)"))
        journal = {}
    fields = journal.get("required_fields")
    if not isinstance(fields, list):
        issues.append(_issue("gated_journal_fields", "gated_sources.journal.required_fields",
                             "required_fields must be a list"))
        fields = []
    missing_fields = [field for field in GATED_JOURNAL_FIELDS if field not in fields]
    if missing_fields:
        issues.append(_issue("gated_journal_missing", "gated_sources.journal.required_fields",
                             f"access journal misses {missing_fields}"))
    if not _text(journal.get("rule")):
        issues.append(_issue("gated_journal_rule", "gated_sources.journal.rule",
                             "journal rule is required"))

    no_bypass = block.get("no_bypass")
    if not isinstance(no_bypass, list) or len(no_bypass) < len(GATED_NO_BYPASS_TOPICS):
        issues.append(_issue("gated_no_bypass", "gated_sources.no_bypass",
                             "no_bypass must list the prohibited bypass paths"))
        no_bypass = []
    joined = " ".join(item for item in no_bypass if isinstance(item, str)).lower()
    for topic in GATED_NO_BYPASS_TOPICS:
        if topic not in joined:
            issues.append(_issue("gated_no_bypass_topic", "gated_sources.no_bypass",
                                 f"no_bypass must cover {topic!r}"))

    credentials = block.get("credentials")
    if not isinstance(credentials, dict):
        issues.append(_issue("gated_credentials", "gated_sources.credentials",
                             "credentials block is required"))
    else:
        if credentials.get("in_repo") is not False:
            issues.append(_issue("gated_credentials_repo", "gated_sources.credentials.in_repo",
                                 "credentials.in_repo must be false: no secrets in the repository"))
        if not _text(credentials.get("storage")) or not _text(credentials.get("rule")):
            issues.append(_issue("gated_credentials_rule", "gated_sources.credentials",
                                 "credentials need storage and rule"))

    rules = block.get("rules")
    if not isinstance(rules, list) or len(rules) < 3:
        issues.append(_issue("gated_rules", "gated_sources.rules",
                             "at least 3 gated rules are required (recognise, request, never bypass)"))

    # The two playbooks must exist, be substantial and carry no bypass recipe or secret.
    for name, markers in GATED_MARKERS.items():
        path = org_dir / "playbooks" / name
        if not path.is_file():
            issues.append(_issue("gated_playbook_missing", "playbooks",
                                 f"gated playbook not found: {name}"))
            continue
        text = path.read_text(encoding="utf-8")
        if len(text) < 800:
            issues.append(_issue("gated_playbook_short", "playbooks",
                                 f"gated playbook too short: {name}"))
        low = text.lower()
        for marker in markers:
            if marker.lower() not in low:
                issues.append(_issue("gated_marker", "playbooks",
                                     f"{name}: missing gated marker {marker!r}"))
        for kind in find_credentials(text):
            issues.append(_issue("gated_credential", "playbooks",
                                 f"{name}: credential-like value ({kind}); keep it out of the repo"))
        for recipe in find_bypass_recipes(text):
            issues.append(_issue("gated_bypass_recipe", "playbooks",
                                 f"{name}: bypass recipe {recipe!r} is not allowed"))
    return issues


def check_document(org: dict, org_dir: Path) -> list[dict]:
    """Checks of the org-level document: schema, process, boundaries, evidence, playbooks."""
    issues: list[dict] = []
    if _text(org.get("schema")) != ORG_SCHEMA:
        issues.append(_issue("schema", "schema", f"schema must be {ORG_SCHEMA!r}"))
    if not VERSION_RE.match(_text(org.get("version"))):
        issues.append(_issue("version", "version", "version must look like 1.0.0"))
    if _text(org.get("status")) != "RESEARCH_ONLY":
        issues.append(_issue("status", "status", "status must stay RESEARCH_ONLY"))
    if org.get("admission") is not False:
        issues.append(_issue("admission", "admission", "admission must be false (fail-closed)"))
    if _text(org.get("mode")) != ORG_MODE:
        issues.append(_issue("mode", "mode", f"mode must be {ORG_MODE!r} (OSINT-only build)"))
    if org.get("subscriptions") != 0:
        issues.append(_issue("subscriptions", "subscriptions",
                             "subscriptions must be 0 in OSINT_ONLY: no paid access exists"))
    rules = org.get("mode_rules")
    if not isinstance(rules, dict):
        issues.append(_issue("mode_rules", "mode_rules", "mode_rules block is required"))
    else:
        if tuple(rules.get("allowed_source_classes") or ()) != OPEN_SOURCE_CLASSES:
            issues.append(_issue("mode_allowed", "mode_rules.allowed_source_classes",
                                 f"allowed_source_classes must be {list(OPEN_SOURCE_CLASSES)}"))
        if tuple(rules.get("deferred_source_classes") or ()) != DEFERRED_SOURCE_CLASSES:
            issues.append(_issue("mode_deferred", "mode_rules.deferred_source_classes",
                                 f"deferred_source_classes must be {list(DEFERRED_SOURCE_CLASSES)}"))
        if tuple(rules.get("forbidden_source_classes") or ()) != FORBIDDEN_SOURCE_CLASSES:
            issues.append(_issue("mode_forbidden", "mode_rules.forbidden_source_classes",
                                 f"forbidden_source_classes must be {list(FORBIDDEN_SOURCE_CLASSES)}"))
        if rules.get("subscriptions") != 0:
            issues.append(_issue("mode_subscriptions", "mode_rules.subscriptions",
                                 "mode_rules.subscriptions must be 0"))
        if _text(rules.get("licensed_role_status")) not in LICENSED_ROLE_STATUSES:
            issues.append(_issue("mode_licensed", "mode_rules.licensed_role_status",
                                 f"licensed_role_status must be one of {list(LICENSED_ROLE_STATUSES)}"))
        if _text(rules.get("report_field")) != "mode":
            issues.append(_issue("mode_report", "mode_rules.report_field",
                                 "report_field must be 'mode': every report prints its mode"))
        if _text(rules.get("default_cli_mode")) not in MODES:
            issues.append(_issue("mode_cli", "mode_rules.default_cli_mode",
                                 f"default_cli_mode must be one of {list(MODES)}"))
        if not _text(rules.get("rule")):
            issues.append(_issue("mode_rule_text", "mode_rules.rule", "mode_rules.rule is required"))
    policy = org.get("owner_policy")
    if not isinstance(policy, dict):
        issues.append(_issue("owner_policy_block", "owner_policy",
                             "owner_policy block is required: no paid licences, data only via OSINT"))
    else:
        if _text(policy.get("decision")) != "NO_PAID_LICENCES":
            issues.append(_issue("owner_policy_decision", "owner_policy.decision",
                                 "owner_policy.decision must stay NO_PAID_LICENCES"))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", _text(policy.get("date"))):
            issues.append(_issue("owner_policy_date", "owner_policy.date",
                                 "owner_policy.date must be an ISO date"))
        if not _text(policy.get("statement")):
            issues.append(_issue("owner_policy_statement", "owner_policy.statement",
                                 "owner_policy.statement is required"))
        if not isinstance(policy.get("rules"), list) or len(policy["rules"]) < 4:
            issues.append(_issue("owner_policy_rules", "owner_policy.rules",
                                 "owner_policy.rules must list at least 4 rules"))
        if not isinstance(policy.get("enforcement"), list) or not policy["enforcement"]:
            issues.append(_issue("owner_policy_enforcement", "owner_policy.enforcement",
                                 "owner_policy.enforcement must point to tests/docs"))
    licensed = org.get("licensed_data")
    if not isinstance(licensed, dict):
        issues.append(_issue("licensed_block", "licensed_data",
                             "licensed_data block is required (deferred, not deleted)"))
    else:
        if _text(licensed.get("role")) != LICENSED_ROLE:
            issues.append(_issue("licensed_role", "licensed_data.role",
                                 f"licensed_data.role must be {LICENSED_ROLE!r}"))
        if _text(licensed.get("status")) not in LICENSED_ROLE_STATUSES:
            issues.append(_issue("licensed_status", "licensed_data.status",
                                 f"licensed_data.status must be one of "
                                 f"{list(LICENSED_ROLE_STATUSES)}"))
        if licensed.get("subscriptions") != 0:
            issues.append(_issue("licensed_subscriptions", "licensed_data.subscriptions",
                                 "licensed_data.subscriptions must be 0"))
        if not _text(licensed.get("reason")):
            issues.append(_issue("licensed_reason", "licensed_data.reason",
                                 "licensed_data.reason is required"))
        checklist = licensed.get("activation_checklist")
        if not isinstance(checklist, list) or not checklist:
            issues.append(_issue("licensed_checklist", "licensed_data.activation_checklist",
                                 "licensed_data needs an activation checklist"))
    if not _text(org.get("purpose")):
        issues.append(_issue("purpose", "purpose", "purpose is required"))

    process = org.get("process")
    if not isinstance(process, dict):
        issues.append(_issue("process", "process", "process must be an object"))
        process = {}
    order = process.get("order")
    if not isinstance(order, list) or tuple(order) != PROCESS_ORDER:
        issues.append(_issue("process_order", "process.order",
                             f"process.order must be {list(PROCESS_ORDER)}"))
    states = process.get("states")
    if not isinstance(states, list) or tuple(states) != PROCESS_STATES:
        issues.append(_issue("process_states", "process.states",
                             f"process.states must be {list(PROCESS_STATES)}"))
    owners = process.get("owners") or {}
    expected_owners = {
        "question": "director", "source_plan": "director", "collect": "osint-scout",
        "grade": "evidence-qa", "analysis": "segment-role", "case": "case-writer",
        "qa": "evidence-qa", "kb": "kb-librarian",
    }
    for step, owner in expected_owners.items():
        if owners.get(step) != owner:
            issues.append(_issue("process_owner", f"process.owners.{step}",
                                 f"owner must be {owner!r}, got {owners.get(step)!r}"))
    if not _text(process.get("exit_rule")):
        issues.append(_issue("process_exit", "process.exit_rule", "exit_rule is required"))

    evidence = org.get("evidence")
    if not isinstance(evidence, dict):
        issues.append(_issue("evidence", "evidence", "evidence block is required"))
    else:
        if tuple(evidence.get("grades") or ()) != GRADES:
            issues.append(_issue("evidence_grades", "evidence.grades",
                                 f"grades must be {list(GRADES)}"))
        if tuple(evidence.get("statuses") or ()) != STATUSES:
            issues.append(_issue("evidence_statuses", "evidence.statuses",
                                 f"statuses must be {list(STATUSES)}"))
        rank = evidence.get("rank") or {}
        if rank != STATUS_RANK:
            issues.append(_issue("evidence_rank", "evidence.rank",
                                 "rank must order NOT_FETCHED < HYPOTHESIS < PARTIAL < VERIFIED"))
        if not isinstance(evidence.get("verified_requires"), list) or \
                len(evidence.get("verified_requires") or []) < 3:
            issues.append(_issue("evidence_requires", "evidence.verified_requires",
                                 "verified_requires must list the conditions for VERIFIED"))

    boundaries = org.get("boundaries")
    if not isinstance(boundaries, dict):
        issues.append(_issue("boundaries", "boundaries", "boundaries block is required"))
    else:
        for key in REQUIRED_BOUNDARIES:
            if key not in boundaries:
                issues.append(_issue("boundary_missing", f"boundaries.{key}",
                                     "boundary rule is required"))
            elif isinstance(boundaries[key], str) and not boundaries[key].strip():
                issues.append(_issue("boundary_empty", f"boundaries.{key}", "boundary rule is empty"))
            elif boundaries[key] in (None, False, [], {}):
                issues.append(_issue("boundary_empty", f"boundaries.{key}", "boundary rule is empty"))
        allowed = boundaries.get("allowed_sources")
        if not isinstance(allowed, list) or len(allowed) < 3:
            issues.append(_issue("boundary_sources", "boundaries.allowed_sources",
                                 "allowed_sources must list at least 3 lawful classes"))

    if not isinstance(org.get("non_goals"), list) or len(org.get("non_goals") or []) < 3:
        issues.append(_issue("non_goals", "non_goals", "non_goals must list at least 3 items"))
    if not isinstance(org.get("review_gates"), list) or len(org.get("review_gates") or []) < 4:
        issues.append(_issue("review_gates", "review_gates", "at least 4 review gates are required"))
    if not isinstance(org.get("kpi"), list) or not org.get("kpi"):
        issues.append(_issue("kpi", "kpi", "kpi must be a non-empty list"))
    for deferred in org.get("deferred_roles") or []:
        if not isinstance(deferred, dict):
            issues.append(_issue("deferred_type", "deferred_roles", "deferred role must be an object"))
            continue
        if not _text(deferred.get("id")) or not _text(deferred.get("reason")):
            issues.append(_issue("deferred_fields", "deferred_roles",
                                 "deferred role needs id and reason"))
        if not isinstance(deferred.get("activation_checklist"), list) or \
                not deferred.get("activation_checklist"):
            issues.append(_issue("deferred_checklist", "deferred_roles",
                                 "deferred role needs an activation checklist"))

    markers = {
        "source_hierarchy.md": SOURCE_HIERARCHY_MARKERS,
        "lawful_osint.md": LAWFUL_OSINT_MARKERS,
        "red_lines.md": RED_LINES_MARKERS,
    }
    for name in PLAYBOOKS:
        path = org_dir / "playbooks" / name
        if not path.is_file():
            issues.append(_issue("playbook_missing", "playbooks", f"playbook not found: {name}"))
            continue
        text = path.read_text(encoding="utf-8")
        if len(text) < 400:
            issues.append(_issue("playbook_short", "playbooks", f"playbook too short: {name}"))
        for marker in markers.get(name, ()):
            if marker not in text:
                issues.append(_issue("playbook_marker", "playbooks",
                                     f"{name}: missing OSINT-only marker {marker!r}"))
        for kind in find_credentials(text):
            issues.append(_issue("credential", "playbooks",
                                 f"{name}: credential-like value ({kind}); keep it out of the repo"))

    for kind in find_credentials(json.dumps(org, ensure_ascii=False)):
        issues.append(_issue("credential", "gated_sources",
                             f"credential-like value ({kind}) in org.json; "
                             "store secrets in a secret manager, never in the repo"))
    issues.extend(check_gated(org, org_dir))
    return issues


def _normalise_mode(mode: str) -> str:
    """Only ``osint`` is supported while the network has no licences (fail-closed)."""
    value = (mode or "").strip().lower()
    if value not in MODES:
        raise OrgError(f"unsupported mode {mode!r}: this build is OSINT-only, use --mode osint")
    return value


def check(org_path: Path = DEFAULT_ORG, accounts_path: Path = DEFAULT_ACCOUNTS,
          mode: str = DEFAULT_MODE) -> dict:
    """Run every check and return the machine summary (raises :class:`OrgError`)."""
    mode = _normalise_mode(mode)
    org_path = Path(org_path)
    org = load_org(org_path)
    org_dir = org_path.parent
    accounts_path = Path(accounts_path)
    accounts = load_accounts(accounts_path)
    accounts_error = None if accounts else f"{accounts_path} missing or empty"
    issues = check_document(org, org_dir) + check_roles(org, org_dir, accounts, accounts_error)
    issues.sort(key=lambda item: (item["role"] or "", item["code"], item["field"]))
    roles = [role for role in org.get("roles") or [] if isinstance(role, dict)]
    layers: dict[str, int] = {layer: 0 for layer in LAYERS}
    for role in roles:
        if role.get("layer") in layers:
            layers[role["layer"]] += 1
    assigned: dict[str, str] = {}
    for role in roles:
        for account in role.get("accounts") or []:
            if isinstance(account, str):
                assigned.setdefault(account, _text(role.get("id")))
    gated_block = org.get("gated_sources") if isinstance(org.get("gated_sources"), dict) else {}
    gated_classes = [entry for entry in (gated_block.get("classes") or []) if isinstance(entry, dict)]
    gated_summary = {
        "schema": _text(gated_block.get("schema")),
        "playbook": _text(gated_block.get("playbook")),
        "access_request": _text(gated_block.get("access_request_template")),
        "statuses": list(gated_block.get("statuses") or []),
        "classes": [
            {
                "id": entry.get("id"),
                "access": entry.get("access"),
                "source_classes": entry.get("source_classes") or [],
                "collect_now": entry.get("collect_now"),
                "request_required": entry.get("request_required"),
            }
            for entry in gated_classes
        ],
        "collect_now": [_text(entry.get("id")) for entry in gated_classes
                        if entry.get("collect_now") is True],
        "request_only": [_text(entry.get("id")) for entry in gated_classes
                         if entry.get("request_required") is True],
        "never": [_text(entry.get("id")) for entry in gated_classes
                  if _text(entry.get("access")) == "NO_ACCESS"],
        "journal_fields": list((gated_block.get("journal") or {}).get("required_fields") or []),
        "credentials_in_repo": bool((gated_block.get("credentials") or {}).get("in_repo")),
    }
    return {
        "schema": CHECK_SCHEMA,
        "org": str(org_path),
        "org_version": org.get("version"),
        "org_status": org.get("status"),
        "mode": mode,
        "org_mode": org.get("mode"),
        "subscriptions": org.get("subscriptions"),
        "source_classes": {
            "allowed": (org.get("mode_rules") or {}).get("allowed_source_classes") or [],
            "deferred": (org.get("mode_rules") or {}).get("deferred_source_classes") or [],
            "forbidden": (org.get("mode_rules") or {}).get("forbidden_source_classes") or [],
        },
        "licensed_data": {
            "role": LICENSED_ROLE,
            "status": _text((org.get("licensed_data") or {}).get("status")),
        },
        "valid": not issues,
        "errors": len(issues),
        "counts": {
            "roles": len(roles),
            "lead": 1 if org.get("lead") else 0,
            "layers": dict(layers),
            "segments": len(org.get("segments") or []),
            "accounts_in_portfolio": len(accounts),
            "accounts_assigned": len(assigned),
        },
        "roles": [
            {
                "id": role.get("id"),
                "title": role.get("title"),
                "layer": role.get("layer"),
                "segments": role.get("segments") or [],
                "accounts": role.get("accounts") or [],
                "evidence_ceiling": role.get("evidence_ceiling"),
                "review_role": role.get("review_role"),
                "prompt": role.get("prompt"),
            }
            for role in roles
        ],
        "process": (org.get("process") or {}).get("order"),
        "boundaries": sorted((org.get("boundaries") or {}).keys()),
        "gated": gated_summary,
        "issues": issues,
        "policy": {
            "mode": mode,
            "org_mode": org.get("mode"),
            "subscriptions": 0,
            "licensed_data": _text((org.get("licensed_data") or {}).get("status")),
            "gated": "G0/G1 open; G2/G3 request-only; G4 never; credentials out of repo",
            "network": "disabled",
            "writes": "none (only --out writes the requested brief)",
            "model_status": "RESEARCH_ONLY",
            "admission": False,
        },
    }


def render_org(summary: dict) -> str:
    """Human-readable org chart, process and boundaries."""
    lines = [
        f"Market Intelligence: {summary['counts']['roles']} узлов "
        f"(1 lead + {summary['counts']['roles'] - 1} ролей), схема {summary['org_version']}, "
        f"статус {summary['org_status']}",
        f"Режим: {summary.get('org_mode')} (`--mode {summary.get('mode')}`) · подписок: "
        f"{summary.get('subscriptions')} · licensed-data: {summary.get('licensed_data', {}).get('status')}",
        f"Источники: разрешены {summary.get('source_classes', {}).get('allowed')}, "
        f"DEFERRED {summary.get('source_classes', {}).get('deferred')}, "
        f"запрещены {summary.get('source_classes', {}).get('forbidden')}",
        f"Gated: собираем {summary.get('gated', {}).get('collect_now')}, по запросу "
        f"{summary.get('gated', {}).get('request_only')} (договор/NDA), без доступа "
        f"{summary.get('gated', {}).get('never')}; ключи вне репозитория",
        f"Сегментов: {summary['counts']['segments']}; аккаунтов в портфеле: "
        f"{summary['counts']['accounts_in_portfolio']}, назначено ролям: "
        f"{summary['counts']['accounts_assigned']}",
        "",
        "Процесс: " + " -> ".join(step.upper() for step in summary["process"]),
        "",
        f"{'роль':<24}{'слой':<12}{'evidence':<12}{'review':<16}аккаунты",
        "-" * 88,
    ]
    for role in summary["roles"]:
        status = " (DEFERRED)" if role["id"] == LICENSED_ROLE else ""
        lines.append(f"{role['id']:<24}{role['layer']:<12}{role['evidence_ceiling']:<12}"
                     f"{role['review_role']:<16}{len(role['accounts'])}{status}")
    lines.append("")
    lines.append("Границы: " + ", ".join(summary["boundaries"]))
    if summary["issues"]:
        lines.append("")
        lines.append(f"Ошибки ({summary['errors']}):")
        for issue in summary["issues"]:
            where = f"[{issue['role']}] " if issue["role"] else ""
            lines.append(f"  {where}{issue['code']}: {issue['field']}: {issue['message']}")
    else:
        lines.append("")
        lines.append("Проверка пройдена: роли, промпты, сегменты, аккаунты, review, границы.")
    lines.append("")
    lines.append("Бриф роли: python tools/market_intel.py --role <id> --question \"...\" "
                 "[--account <ID>] [--out path]")
    return "\n".join(lines)


def _account_block(account_id: str, accounts: dict[str, dict]) -> tuple[str, list[str]]:
    """Render the account section; the internal card is HYPOTHESIS, never a fact."""
    if not account_id:
        return ("Аккаунт: не указан (сегментный/агрегированный вопрос). Данные об аккаунте не\n"
                "подставляются; при необходимости Director назначает аккаунт.", [])
    record = accounts.get(account_id)
    if record is None:
        raise OrgError(f"unknown account {account_id!r}; "
                       f"known: {', '.join(sorted(accounts)) or '(accounts file unavailable)'}")
    lines = [
        f"Аккаунт: `{account_id}` — {record.get('name', '')}".rstrip(),
        f"Внутренняя карточка (marketing/target_accounts.json): segment={record.get('segment')}, "
        f"hq={record.get('hq')}",
        "Evidence: HYPOTHESIS — это экспертная оценка AMFE, а не факт о компании. Первоисточник не",
        "получен (NOT_FETCHED). Любое утверждение об аккаунте требует источника и даты.",
    ]
    use_cases = record.get("use_cases") or []
    if use_cases:
        lines.append("Use cases в карточке (гипотеза): " + ", ".join(use_cases))
    return "\n".join(lines), use_cases


def build_brief(org: dict, role_id: str, question: str, account_id: str = "",
                accounts: dict[str, dict] | None = None,
                accounts_error: str | None = None, mode: str = DEFAULT_MODE) -> str:
    """Build a deterministic OSINT-only task brief for one role. No network, no invented data."""
    mode = _normalise_mode(mode)
    roles = [role for role in org.get("roles") or [] if isinstance(role, dict)]
    role = next((item for item in roles if item.get("id") == role_id), None)
    if role is None:
        raise OrgError(f"unknown role {role_id!r}; known: {', '.join(_text(r.get('id')) for r in roles)}")
    question = (question or "").strip()
    if len(question) < 12:
        raise OrgError("question is too short: need a decision-relevant question (>= 12 chars)")
    accounts = accounts or {}
    account_block, use_cases = _account_block(account_id, accounts)

    reviewer = _text(role.get("review_role"))
    lines = [
        f"# Task brief: {role.get('title')} (`{role_id}`)",
        "",
        f"Схема сети: {org.get('schema')} v{org.get('version')} · статус {org.get('status')} · "
        "admission=false",
        f"Mode: {mode} ({org.get('mode')}): подписок {org.get('subscriptions')}; разрешены S1–S3, "
        "S4 — DEFERRED, S5 — запрещён.",
        "Сеть не выполняет сетевых вызовов: адаптеры описываются, данные не выдумываются.",
        "",
        "## Вопрос",
        question,
        "",
        "## Аккаунт",
        account_block,
        "",
        "## Мандат роли",
        _text(role.get("mandate")),
        "",
        "## Что читать до работы",
    ]
    for item in role.get("inputs") or []:
        lines.append(f"- `{item}`")
    if role.get("questions"):
        lines.append("")
        lines.append("## Подвопросы, которые роль обязана закрыть или пометить NOT_FETCHED")
        for item in role["questions"]:
            lines.append(f"- {item}")
    lines += [
        "",
        "## Source plan (OSINT: только S1–S3; заполнить до сбора, не после)",
        "",
        "| # | что ищем | класс источника | ожидаемый grade | доступ | статус |",
        "|---:|---|---|---|---|---|",
        "| 1 | периметр и профиль аккаунта по первоисточнику | S2: раскрытие/отчётность/сайт "
        "компании | A | OPEN, бесплатно | NOT_FETCHED |",
        "| 2 | регуляторный/статистический факт (добыча, цены, ставки) | S1: регулятор и статистика "
        "(SEC/EDGAR, EIA, IEA public, World Bank, OECD) | A | OPEN, бесплатно | NOT_FETCHED |",
        "| 3 | отраслевой фон и ограничения | S3: отраслевое издание, ассоциация, деловые новости | "
        "B | OPEN, бесплатно | NOT_FETCHED |",
        "| — | лицензированные БД и закрытые источники | S4 / S5 | — | DEFERRED / FORBIDDEN | "
        "NOT_FETCHED — не собираем |",
        "",
        "Подписок нет: S4 не используется, S5 запрещён, paywall не обходится. Порядок источников — "
        "`playbooks/source_hierarchy.md`. Строка без источника не становится фактом: она остаётся "
        "HYPOTHESIS или NOT_FETCHED.",
        "Gated-классы: G0 public и G1 registration — собираем в пределах ToS; G2 licensed и G3 NDA "
        "data room — только access request (`playbooks/access_request.md`) и решение человека/Lead; "
        "G4 restricted — NOT_AUTHORIZED, не трогаем. Обход не рассматривается как вариант.",
        "",
        "## Что уже известно (и чего нет)",
        f"- Внутренняя карточка аккаунта: HYPOTHESIS (не факт); use_cases: "
        f"{', '.join(use_cases) if use_cases else 'не указаны'}",
        "- Внешние данные: NOT_FETCHED — в среде нет доступа к сети; подписки нет, S4/S5 не "
        "используются." + (f" ({accounts_error})" if accounts_error else ""),
        "- Оценки AMFE (fit/digital/access/budget/risk) — экспертные, не рейтинг компании; "
        "в кейс как факт не выносятся.",
        "",
        "## Порядок работы",
        "1. Уточнить у Director подвопросы и evidence ceiling (если brief их не задаёт).",
        "2. Заполнить source plan: класс источника (S1–S3) -> ожидаемый grade -> доступность.",
        "3. Собрать с URL, датой и цитатой; недоступное — NOT_FETCHED с причиной и блокером.",
        "4. Передать evidence-таблицу в `evidence-qa` до анализа: статус ставит QA, не автор.",
        "5. Строить сценарии/допущения только вокруг подтверждённого.",
        "",
        "## Формат отчёта",
        "```text",
        "Mode: osint (OSINT_ONLY); подписок 0; S4/S5 не использовались",
        "Аккаунт / подвопрос",
        "Evidence-таблица: | # | утверждение | класс S1–S3 | grade A/B/C | статус | source_id | "
        "retrieved_at | цитата |",
        "NOT_FETCHED: | что искали | класс источника | причина | блокер |",
        "Access journal: | request_id | источник | G0–G4 | основание | decision | expires_at |",
        "NOT_AUTHORIZED: источник, класс G, причина, blocker",
        "Анализ: сценарии + допущения + что меняет решение",
        "Что не проверено",
        "Red lines: проверено, нарушений нет (или RL-xx + процедура остановки)",
        "```",
        "",
        "## Критерий приёмки",
        "- каждая строка имеет grade, статус и источник либо NOT_FETCHED с причиной;",
        "- источник класса S1–S3; S4/S5 отсутствуют, paywall не обходился;",
        "- ни одной цифры без источника и даты; допущение помечено;",
        "- нет персональных данных, контактов и обещаний прогноза/допуска;",
        "- отчёт отвечает на вопрос brief, а не на соседний.",
        "",
        "## Передача",
        f"`{reviewer}` — на проверку; затем Director и `kb-librarian` для записи в базу знаний "
        "(запись валидируется `tools/kb.py`). `licensed-data` в OSINT_ONLY — DEFERRED.",
        "",
        "## Запрещено",
    ]
    for item in role.get("forbidden") or []:
        lines.append(f"- {item}")
    lines += [
        "- обход авторизации/paywall/robots/terms/лимитов, утёкшие базы, чужие аккаунты;",
        "- S4 без договора (подписок нет) и S5 в любом виде; платный контент в обход paywall;",
        "- персональные данные, деанонимизация, покупка контактов, спам;",
        "- подкуп, оплата за конфиденциальную информацию, обход санкционных ограничений;",
        "- выдумывание данных, цифр и источников; выдача гипотезы за факт;",
        "- публикация, отправка, коммит: интеграция — только Lead.",
        "",
    ]
    body = "\n".join(lines)
    text = body + (f"_Generated by tools/market_intel.py (no network). "
                   f"Brief body hash: {_sha256(body)}_\n")
    _scan_brief(text)
    return text


def build_access_request(org: dict, source: str, gated_class: str, purpose: str,
                         legal_basis: str, role_id: str = OSINT_ROLE) -> str:
    """Draft a lawful access request for a G2/G3 source. No network, no credentials, no approval."""
    gated = (gated_class or "").strip().upper()
    if gated not in GATED_CLASSES:
        raise OrgError(f"unknown gated class {gated_class!r}; expected one of {list(GATED_CLASSES)}")
    if gated == "G0":
        raise OrgError("G0 is public: no access request is needed (check robots/ToS and cite)")
    if gated == "G4":
        raise OrgError("G4 has no lawful access path: record NOT_AUTHORIZED, do not request access")
    source = (source or "").strip()
    purpose = (purpose or "").strip()
    legal_basis = (legal_basis or "").strip()
    if len(source) < 2:
        raise OrgError("source is too short: name the gated source")
    if len(purpose) < 12:
        raise OrgError("purpose is too short: name the decision-relevant question (>= 12 chars)")
    if len(legal_basis) < 8:
        raise OrgError("legal_basis is too short: name the contract/NDA/FOIA/ToS basis (>= 8 chars)")
    roles = [role for role in org.get("roles") or [] if isinstance(role, dict)]
    known = {_text(role.get("id")) for role in roles}
    if role_id not in known:
        raise OrgError(f"unknown role {role_id!r}; known: {', '.join(sorted(known))}")
    if role_id not in (OSINT_ROLE, "director"):
        raise OrgError("an access request is filed by osint-scout and reviewed by director")
    entries = {_text(entry.get("id")): entry
               for entry in ((org.get("gated_sources") or {}).get("classes") or [])
               if isinstance(entry, dict)}
    entry = entries.get(gated, {})
    title = _text(entry.get("title_ru")) or _text(entry.get("title")) or gated
    requirements = [item for item in (entry.get("requirements") or []) if isinstance(item, str)]
    lines = [
        f"# Access request: {source} ({gated})",
        "",
        f"Схема: {ACCESS_REQUEST_SCHEMA} · сеть: {org.get('mode')} · статус: REQUEST_PENDING",
        "Агент подготовил черновик; решение и отправку выполняет человек/Lead. Агент не подписывает",
        "договор, не принимает NDA и не получает ключи.",
        "",
        "## Кто",
        f"- Роль-заявитель: `{role_id}` (без имён и личных контактов)",
        "- Владелец запроса: `director`; подписант: человек/Lead",
        "",
        "## Источник",
        f"- Название: {source}",
        f"- Gated-класс: {gated} — {title}",
        "- Класс источника S: S4 (licensed dataset)",
        f"- Что требуется по политике: {'; '.join(requirements) if requirements else 'см. playbooks/gated_sources.md'}",
        "",
        "## Правовое основание",
        legal_basis,
        "",
        "## Цель",
        purpose,
        "",
        "## Срок",
        "- Срок доступа: заполняет человек при подписании (с даты ... по дату ...)",
        "- Пересмотр: не позднее окончания срока; бессрочного доступа нет",
        "",
        "## Что не копируем",
        "- полные тексты и сырые выгрузки в репозиторий;",
        "- персональные данные и контакты частных лиц;",
        "- содержимое data room сверх разрешённого объёма;",
        "- ключи, токены, cookie и логины — только в секрет-стор, выдаёт человек.",
        "",
        "## Как используем",
        "- внутренняя аналитика по вопросу из раздела «Цель»;",
        "- цитата <= 300 символов с идентификатором и датой;",
        "- передача клиенту — только если это прямо разрешено договором.",
        "",
        "## Решение",
        "- После ответа: `AUTHORIZED` / `NOT_AUTHORIZED` / уточнение (`REQUEST_PENDING`).",
        "- Отказ фиксируется как `NOT_AUTHORIZED` + `NOT_FETCHED` с блокером; источник не используется.",
        "- После `AUTHORIZED` режим сети не меняется: снятие OSINT_ONLY — решение человека/Lead",
        "  с правкой org.json, тестов и docs/212.",
        "",
        "## Журнал",
        "Запись: request_id, role, source, gated_class, legal_basis, purpose, requested_at, decision,",
        "decided_at, expires_at, owner. Ключи и персональные данные в журнал не попадают.",
        "",
        "## Напоминание",
        "Обход paywall, авторизации, NDA, robots/terms и лимитов запрещён. Доступ либо есть законно,",
        "либо его нет: `NOT_AUTHORIZED` — нормальный результат.",
        "",
    ]
    body = "\n".join(lines)
    text = body + (f"_Generated by tools/market_intel.py (no network). "
                   f"Access request body hash: {_sha256(body)}_\n")
    _scan_document(text, "access request")
    return text


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scan_document(text: str, label: str) -> None:
    """Defensive scan: an emitted document must not leak contacts or admit a model."""
    if EMAIL_RE.search(text):
        raise OrgError(f"{label} would contain an e-mail address; refusing to emit it")
    if PHONE_RE.search(text):
        raise OrgError(f"{label} would contain a phone-like number; refusing to emit it")
    if ADMISSION_RE.search(text):
        raise OrgError(f"{label} would mention admission; refusing to emit it")


def _scan_brief(text: str) -> None:
    """Backwards-compatible wrapper around :func:`_scan_document` for briefs."""
    _scan_document(text, "brief")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AMFE market-intelligence org validator / brief builder")
    parser.add_argument("--org-file", default=str(DEFAULT_ORG), help="path to org.json")
    parser.add_argument("--accounts-file", default=str(DEFAULT_ACCOUNTS),
                        help="path to marketing/target_accounts.json")
    parser.add_argument("--mode", default=DEFAULT_MODE,
                        help="source mode: 'osint' (default and only supported; OSINT_ONLY)")
    parser.add_argument("--check", action="store_true", help="validate; exit 2 on any issue")
    parser.add_argument("--org", action="store_true", help="print the org chart and process")
    parser.add_argument("--json", action="store_true", help="machine summary as JSON")
    parser.add_argument("--role", help="role id for a task brief")
    parser.add_argument("--question", default="", help="decision-relevant question for the brief")
    parser.add_argument("--account", default="", help="optional target account id (e.g. TATNEFT)")
    parser.add_argument("--out", help="write the brief/access request to this file instead of stdout")
    parser.add_argument("--access-request", action="store_true",
                        help="draft a lawful access request for a G2/G3 gated source")
    parser.add_argument("--source", default="", help="gated source name for --access-request")
    parser.add_argument("--gated-class", default="", help="gated class for --access-request (G2 or G3)")
    parser.add_argument("--purpose", default="", help="decision-relevant purpose for the request")
    parser.add_argument("--legal-basis", default="",
                        help="contract/NDA/FOIA/ToS basis for the access request")
    args = parser.parse_args(argv)

    org_path = Path(args.org_file)
    accounts_path = Path(args.accounts_file)
    accounts = load_accounts(accounts_path)
    accounts_error = None if accounts else f"{accounts_path} unavailable"

    try:
        mode = _normalise_mode(args.mode)
        org = load_org(org_path)
        if args.access_request:
            request = build_access_request(org, args.source, args.gated_class, args.purpose,
                                           args.legal_basis, args.role or OSINT_ROLE)
            if args.out:
                out = Path(args.out)
                if not out.parent.exists():
                    raise OrgError(f"output directory does not exist: {out.parent}")
                out.write_text(request, encoding="utf-8", newline="\n")
                print(f"access request written: {out}")
            elif args.json:
                print(json.dumps({
                    "schema": ACCESS_REQUEST_SCHEMA,
                    "mode": mode,
                    "org_mode": org.get("mode"),
                    "role": args.role or OSINT_ROLE,
                    "source": args.source,
                    "gated_class": (args.gated_class or "").upper(),
                    "purpose": args.purpose,
                    "legal_basis": args.legal_basis,
                    "status": "REQUEST_PENDING",
                    "human_approval_required": True,
                    "network": "disabled",
                    "request": request,
                    "request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
                }, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(request)
            return 0
        if args.role:
            brief = build_brief(org, args.role, args.question, args.account, accounts,
                                accounts_error, mode)
            if args.out:
                out = Path(args.out)
                if not out.parent.exists():
                    raise OrgError(f"output directory does not exist: {out.parent}")
                out.write_text(brief, encoding="utf-8", newline="\n")
                print(f"brief written: {out}")
            elif args.json:
                role = next(r for r in org["roles"] if r.get("id") == args.role)
                print(json.dumps({
                    "schema": "amfe.market_intel.brief.v1",
                    "mode": mode,
                    "org_mode": org.get("mode"),
                    "subscriptions": org.get("subscriptions"),
                    "role": args.role,
                    "title": role.get("title"),
                    "question": args.question,
                    "account": args.account or None,
                    "review_role": role.get("review_role"),
                    "evidence_ceiling": role.get("evidence_ceiling"),
                    "brief": brief,
                    "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
                    "network": "disabled",
                }, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(brief)
            return 0
        summary = check(org_path, accounts_path, mode)
    except OrgError as error:
        print(f"BLOCKED_INPUT: {error}", file=sys.stderr)
        return 2

    code = 0 if summary["valid"] else 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.org:
        print(render_org(summary))
    elif args.check:
        if summary["valid"]:
            print(f"OK: {summary['counts']['roles']} nodes, "
                  f"{summary['counts']['accounts_assigned']}/{summary['counts']['accounts_in_portfolio']} "
                  f"accounts covered, {summary['counts']['segments']} segments, "
                  f"mode={summary['mode']} ({summary['org_mode']}), "
                  f"licensed-data={summary['licensed_data']['status']}, "
                  f"gated=G2/G3 request-only, G4 never, credentials outside repo")
        else:
            print(f"FAIL: {summary['errors']} issue(s)", file=sys.stderr)
            for issue in summary["issues"]:
                where = f"[{issue['role']}] " if issue["role"] else ""
                print(f"  {where}{issue['code']}: {issue['field']}: {issue['message']}",
                      file=sys.stderr)
    else:
        print(render_org(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
