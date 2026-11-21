#!/usr/bin/env python3
"""Validate the AMFE market-intelligence org and generate role task briefs.

The network is **not** used: the tool only reads local JSON/Markdown and never invents facts. It is
fail-closed (any structural problem is an error, exit 2) and deterministic (no clock, no random, no
environment-dependent ordering). Nothing is written unless ``--out`` is given, and then only the
brief file the caller asked for.

The network runs in **OSINT_ONLY** mode: there are no subscriptions, only open source classes
``S1``-``S3`` are allowed, ``S4`` (licensed dataset) is ``DEFERRED`` and ``S5`` (closed source) is
forbidden. ``--check`` enforces that mode and refuses an active licensed-data role.

Commands::

    python tools/market_intel.py --check                 # exit 0 / 2, prints the issues
    python tools/market_intel.py --org                   # human-readable org chart + process
    python tools/market_intel.py --json                  # machine summary (amfe.market_intel.check.v1)
    python tools/market_intel.py --mode osint --check    # explicit mode (osint is the default)
    python tools/market_intel.py --role upstream --question "..." --account TATNEFT
    python tools/market_intel.py --role upstream --question "..." --out .tmp/brief.md

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
PLAYBOOKS = ("lawful_osint.md", "source_hierarchy.md", "evidence_grading.md", "red_lines.md")
SOURCE_HIERARCHY_MARKERS = ("S1", "S2", "S3", "S4", "S5", "OSINT_ONLY", "paywall", "DEFERRED")
LAWFUL_OSINT_MARKERS = ("paywall", "robots", "S1–S3", "OSINT_ONLY", "персональные данные")
RED_LINES_MARKERS = ("RL-19", "RL-20", "RL-21", "RL-22", "OSINT_ONLY", "paywall", "robots")

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
        "issues": issues,
        "policy": {
            "mode": mode,
            "org_mode": org.get("mode"),
            "subscriptions": 0,
            "licensed_data": _text((org.get("licensed_data") or {}).get("status")),
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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scan_brief(text: str) -> None:
    """Defensive scan: the brief must not leak contacts or admit a model."""
    if EMAIL_RE.search(text):
        raise OrgError("brief would contain an e-mail address; refusing to emit it")
    if PHONE_RE.search(text):
        raise OrgError("brief would contain a phone-like number; refusing to emit it")
    if ADMISSION_RE.search(text):
        raise OrgError("brief would mention admission; refusing to emit it")


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
    parser.add_argument("--out", help="write the brief to this file instead of stdout")
    args = parser.parse_args(argv)

    org_path = Path(args.org_file)
    accounts_path = Path(args.accounts_file)
    accounts = load_accounts(accounts_path)
    accounts_error = None if accounts else f"{accounts_path} unavailable"

    try:
        mode = _normalise_mode(args.mode)
        org = load_org(org_path)
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
                  f"licensed-data={summary['licensed_data']['status']}")
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
