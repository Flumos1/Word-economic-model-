#!/usr/bin/env python3
"""Validate the AMFE legal-counsel package and generate legal research briefs.

The tool is deliberately narrow: it checks that the role package is structurally sound and that every
citation it is given is **complete and locatable**. It does not know the law, does not read the
network and does not give legal advice. Verification of a precedent is reserved to a human
(``human:*``) or to an official locator (``official:*``); anything else is fail-closed.

Guarantees
----------
* no network (no sockets, no HTTP client, no URL fetching);
* no clock (everything is relative to ``org.json`` -> ``as_of``), so output is deterministic;
* no legal advice automation: the brief is a research memo skeleton with a mandatory disclaimer and
  never a client opinion;
* no personal data: generated text is scanned for e-mails, phone-like numbers and IBAN-like strings;
* fail-closed: any structural problem or unverifiable "VERIFIED" citation means exit code 2;
* writes nothing unless ``--out`` is given.

Commands::

    python tools/legal_research.py --check
    python tools/legal_research.py --check --citations .tmp/citations.json
    python tools/legal_research.py --summary
    python tools/legal_research.py --source-plan --jurisdiction eu
    python tools/legal_research.py --role international-counsel --question "..." --jurisdiction intl
    python tools/legal_research.py --role international-counsel --question "..." --json
    python tools/legal_research.py --role international-counsel --question "..." --citations f.json

Exit codes: 0 = ok, 2 = invalid input or failed check (fail-closed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORG = ROOT / "agents" / "legal" / "org.json"
PROTOCOL_ROLES = ROOT / "protocols" / "agent_roles.json"
MI_ORG = ROOT / "agents" / "market_intelligence" / "org.json"

ORG_SCHEMA = "amfe.legal.org.v1"
CHECK_SCHEMA = "amfe.legal.check.v1"
PLAN_SCHEMA = "amfe.legal.source_plan.v1"
BRIEF_SCHEMA = "amfe.legal.brief.v1"
CITATION_SCHEMA = "amfe.legal.citation.v1"

ROLE_IDS = ("international-counsel", "legal-counsel")
DEFAULT_ROLE = "international-counsel"
DEFAULT_JURISDICTION = "intl"

STATUSES = ("VERIFIED", "PARTIAL", "HYPOTHESIS", "NOT_VERIFIED", "NOT_FETCHED")
STATUS_RANK = {"NOT_FETCHED": 0, "NOT_VERIFIED": 1, "HYPOTHESIS": 2, "PARTIAL": 3, "VERIFIED": 4}
LEVELS = ("L1", "L2", "L3", "L4", "L5")
GROUPS = ("public", "private")
SKILL_IDS = ("public-international-law", "private-international-law", "precedent-research",
             "legal-opinion")
BRANCH_SKILLS = ("public-international-law", "private-international-law")
SKILL_LEVELS = ("AWARE", "WORKING", "ADVANCED")
HOLDING_TYPES = ("ratio", "obiter", "dictum", "mixed", "not_applicable")
GOOD_LAW = ("GOOD_LAW", "OVERRULED", "DISTINGUISHED", "PENDING", "UNKNOWN")
OPINION_SECTIONS = ("question", "facts", "sources", "analysis", "risks", "options", "caveats")
PROCESS_ORDER = ("intake", "conflicts", "question", "facts", "sources", "research", "cite_check",
                 "analysis", "risks_options", "human_review", "handoff")
PROCESS_OWNERS = {
    "intake": "lead", "conflicts": "international-counsel", "question": "lead",
    "facts": "international-counsel", "sources": "osint-scout",
    "research": "international-counsel", "cite_check": "international-counsel",
    "analysis": "international-counsel", "risks_options": "international-counsel",
    "human_review": "human-lawyer", "handoff": "lead",
}

REQUIRED_CHARTER_SECTIONS = ("## Мандат", "## Входы", "## Можно", "## Нельзя", "## Формат отчёта",
                             "## Критерий приёмки", "## Передача")
REQUIRED_BOUNDARIES = (
    "no_legal_advice", "no_attorney_client", "no_fake_citations", "citation_verification",
    "no_personal_data", "privilege", "conflicts_of_interest", "sanctions", "no_network",
    "no_signing", "no_filing", "jurisdiction_disclaimer", "unauthorized_practice",
    "confidentiality",
)
REQUIRED_HANDOFFS = ("lead", "osint", "archivist", "human_lawyer")
REQUIRED_ROLE_FIELDS = ("id", "alias", "title", "title_ru", "layer", "mandate", "charter", "skills",
                        "skills_matrix", "memory", "review_role", "human_reviewer",
                        "evidence_ceiling", "advice_ceiling")

PLAYBOOKS = {
    "public": "public_international_law.md",
    "private": "private_international_law.md",
    "precedent": "precedent_research.md",
    "opinion": "legal_opinion_format.md",
    "ethics": "sanctions_and_ethics.md",
}
PLAYBOOK_MARKERS = {
    "public": ("VCLT", "Устав ООН", "санкц", "экспортн", "инвестиц", "ВТО", "права человека",
               "UNCLOS", "ITLOS", "opinio juris"),
    "private": ("Rome I", "Rome II", "Hague", "New York", "ICC", "LCIA", "UNCITRAL", "CISG",
                "GDPR"),
    "precedent": ("ICJ", "PCIJ", "ITLOS", "WTO", "ICSID", "HUDOC", "CURIA", "UNILEX", "CISG",
                  "ratio", "obiter", "no fake citation"),
    "opinion": ("Вопрос", "Факты", "Источники", "Анализ", "Риски", "Опции", "Оговорки",
                "не является юридической консультацией"),
    "ethics": ("санкц", "экспортн", "50", "privilege", "конфликт", "обход",
               "не является юридической консультацией"),
}
MIN_DOMAINS_PUBLIC = 9
MIN_DOMAINS_PRIVATE = 11
MIN_CITATION_FORUMS = 10
MIN_OFFICIAL_HOSTS = 5
MIN_FORBIDDEN_HOSTS = 3
MIN_FAKE_MARKERS = 3

ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
CITATION_ID_RE = re.compile(r"^C-\d{2,3}$")
URL_RE = re.compile(r"^https?://([^/?#]+)", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?:\+\d[\d\s()-]{7,}\d)|\b\d{3}[\s-]\d{3}[\s-]\d{2}[\s-]\d{2}\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
ADMISSION_RE = re.compile(r"\b(admitted|допущенн\w*)\b", re.IGNORECASE)
ADVICE_RE = re.compile(r"(гарантиру\w*|attorney-client|советуем клиенту|рекомендуем клиенту)",
                       re.IGNORECASE)
AI_VERIFIER_RE = re.compile(r"(model|assistant|llm|gpt|chatbot|openai|anthropic|ai-)", re.IGNORECASE)

DISCLAIMER_ANCHOR = "не является юридической консультацией"


class LegalError(ValueError):
    """The package or an input file cannot be used at all (fail-closed input)."""


def _issue(code: str, field: str, message: str) -> dict:
    return {"code": code, "field": field, "message": message}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------

def load_org(path: Path = DEFAULT_ORG) -> dict:
    """Load the legal org JSON; raises :class:`LegalError` when missing or not an object."""
    path = Path(path)
    if not path.exists():
        raise LegalError(f"file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LegalError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise LegalError(f"{path}: top-level value must be an object")
    return data


def load_citations(path: Path) -> list[dict]:
    """Load a citations file: either a list or ``{"citations": [...]}``."""
    path = Path(path)
    if not path.exists():
        raise LegalError(f"citations file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LegalError(f"invalid JSON in {path}: {error}") from error
    if isinstance(data, dict):
        schema = _text(data.get("schema"))
        if schema and schema != CITATION_SCHEMA:
            raise LegalError(f"{path}: schema must be {CITATION_SCHEMA!r}, got {schema!r}")
        rows = data.get("citations")
    else:
        rows = data
    if not isinstance(rows, list):
        raise LegalError(f"{path}: citations must be a list or an object with a 'citations' list")
    return rows


def load_known_roles() -> tuple[set[str], list[str]]:
    """Role ids known to the wider AMFE contour (protocols + market-intelligence network)."""
    ids: set[str] = set()
    sources: list[str] = []
    for path in (PROTOCOL_ROLES, MI_ORG):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for role in _as_list(data.get("roles")):
            if isinstance(role, dict) and _text(role.get("id")):
                ids.add(_text(role.get("id")))
        sources.append(str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path))
    return ids, sources


# --------------------------------------------------------------------------------------------
# Small validators
# --------------------------------------------------------------------------------------------

def _days_in_month(year: int, month: int) -> int:
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    if month in (4, 6, 9, 11):
        return 30
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 29 if leap else 28


def is_iso_date(value: str) -> bool:
    """Strict ``YYYY-MM-DD`` check without using the clock."""
    match = ISO_DATE_RE.match(_text(value))
    if not match:
        return False
    year, month, day = (int(part) for part in match.groups())
    if not 1800 <= year <= 2200:
        return False
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= _days_in_month(year, month)


def host_of(url: str) -> str | None:
    """Extract a lower-case host from an http(s) URL without importing a URL parser."""
    match = URL_RE.match(_text(url))
    if not match:
        return None
    host = match.group(1).split("@")[-1].split(":")[0].strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def host_matches(host: str, patterns) -> bool:
    """Match a host against exact patterns or ``*.example.org`` wildcards."""
    host = _text(host).lower().rstrip(".")
    for pattern in patterns or ():
        value = _text(pattern).lower().rstrip(".")
        if not value:
            continue
        if value.startswith("*."):
            suffix = value[1:]  # ".example.org"
            if host == suffix[1:] or host.endswith(suffix):
                return True
        elif host == value:
            return True
    return False


# --------------------------------------------------------------------------------------------
# Package checks
# --------------------------------------------------------------------------------------------

def _check_role(org: dict, org_dir: Path, known_roles: set[str]) -> list[dict]:
    issues: list[dict] = []
    role = org.get("role")
    if not isinstance(role, dict):
        return [_issue("role", "role", "role block is required")]

    for field in REQUIRED_ROLE_FIELDS:
        if field not in role:
            issues.append(_issue("required", f"role.{field}", f"missing field {field}"))

    role_id = _text(role.get("id"))
    alias = _text(role.get("alias"))
    if role_id not in ROLE_IDS:
        issues.append(_issue("role_id", "role.id", f"role.id must be one of {list(ROLE_IDS)}"))
    if alias not in ROLE_IDS:
        issues.append(_issue("role_alias", "role.alias", f"role.alias must be one of {list(ROLE_IDS)}"))
    if alias and alias == role_id:
        issues.append(_issue("role_alias_same", "role.alias", "alias must differ from the role id"))
    for field in ("title", "title_ru", "mandate"):
        if not _text(role.get(field)):
            issues.append(_issue("empty", f"role.{field}", f"{field} must be non-empty"))
    if _text(role.get("layer")) != "advisory":
        issues.append(_issue("layer", "role.layer", "layer must be 'advisory'"))
    if _text(role.get("advice_ceiling")) != "RESEARCH_MEMO":
        issues.append(_issue("advice_ceiling", "role.advice_ceiling",
                             "advice_ceiling must stay RESEARCH_MEMO (no client opinions)"))
    ceiling = _text(role.get("evidence_ceiling"))
    if ceiling and ceiling not in STATUSES:
        issues.append(_issue("evidence_ceiling", "role.evidence_ceiling",
                             f"evidence_ceiling must be one of {list(STATUSES)}"))
    if STATUS_RANK.get(ceiling, 0) > STATUS_RANK["PARTIAL"]:
        issues.append(_issue("ceiling_cap", "role.evidence_ceiling",
                             "the role cannot certify VERIFIED on its own"))

    memory = _text(role.get("memory"))
    if not memory:
        issues.append(_issue("memory", "role.memory", "memory path is required"))
    elif not (memory.startswith("agents/memory/") and memory.endswith(".jsonl")):
        issues.append(_issue("memory_path", "role.memory",
                             "memory must be agents/memory/<role>.jsonl"))

    reviewer = _text(role.get("review_role"))
    if not reviewer:
        issues.append(_issue("review", "role.review_role", "review_role is required"))
    elif reviewer == role_id:
        issues.append(_issue("self_review", "role.review_role", "the role cannot review itself"))
    elif known_roles and reviewer not in known_roles:
        issues.append(_issue("review_unknown", "role.review_role",
                             f"review_role {reviewer!r} is not known to the AMFE contour"))
    human = _text(role.get("human_reviewer"))
    if human != "human-lawyer":
        issues.append(_issue("human_reviewer", "role.human_reviewer",
                             "human_reviewer must be 'human-lawyer': a human signs the opinion"))

    # charter
    charter = _text(role.get("charter"))
    if not charter:
        issues.append(_issue("charter", "role.charter", "charter path is required"))
    else:
        path = org_dir / charter
        if not path.is_file():
            issues.append(_issue("charter_missing", "role.charter", f"charter not found: {charter}"))
        else:
            text = path.read_text(encoding="utf-8")
            for section in REQUIRED_CHARTER_SECTIONS:
                if section not in text:
                    issues.append(_issue("charter_section", "role.charter",
                                         f"{charter}: missing section {section!r}"))
            if len(text) < 800:
                issues.append(_issue("charter_short", "role.charter",
                                     f"{charter}: too short to be a role charter"))
            if DISCLAIMER_ANCHOR not in text:
                issues.append(_issue("charter_disclaimer", "role.charter",
                                     "the charter must state that the output is not legal advice"))

    skills = _text(role.get("skills"))
    if not skills:
        issues.append(_issue("skills", "role.skills", "skills path is required"))
    else:
        path = org_dir / skills
        if not path.is_file():
            issues.append(_issue("skills_missing", "role.skills", f"skills file not found: {skills}"))
        else:
            text = path.read_text(encoding="utf-8")
            if len(text) < 800:
                issues.append(_issue("skills_short", "role.skills", f"{skills}: too short"))
            for skill_id in SKILL_IDS:
                if skill_id not in text:
                    issues.append(_issue("skills_domain", "role.skills",
                                         f"{skills}: skill {skill_id!r} is not described"))
            if "не является навыком" not in text:
                issues.append(_issue("skills_limits", "role.skills",
                                     f"{skills}: must list what is not a skill of the role"))
    return issues


def _check_skills_matrix(org: dict, domains: dict[str, dict]) -> list[dict]:
    issues: list[dict] = []
    role = org.get("role") or {}
    matrix = role.get("skills_matrix")
    if not isinstance(matrix, list) or not matrix:
        return [_issue("skills_matrix", "role.skills_matrix", "skills_matrix must be a non-empty list")]
    seen: dict[str, dict] = {}
    for entry in matrix:
        if not isinstance(entry, dict):
            issues.append(_issue("skill_type", "role.skills_matrix", "skill must be an object"))
            continue
        skill_id = _text(entry.get("id"))
        if not skill_id:
            issues.append(_issue("skill_id", "role.skills_matrix", "skill id is required"))
            continue
        if skill_id in seen:
            issues.append(_issue("skill_duplicate", "role.skills_matrix",
                                 f"duplicate skill {skill_id!r}"))
        seen[skill_id] = entry
        if not _text(entry.get("title")):
            issues.append(_issue("skill_title", "role.skills_matrix", f"{skill_id}: title required"))
        if _text(entry.get("level")) not in SKILL_LEVELS:
            issues.append(_issue("skill_level", "role.skills_matrix",
                                 f"{skill_id}: level must be one of {list(SKILL_LEVELS)}"))
        declared = _as_list(entry.get("domains"))
        if not all(isinstance(item, str) for item in declared):
            issues.append(_issue("skill_domains", "role.skills_matrix",
                                 f"{skill_id}: domains must be strings"))
            continue
        for domain_id in declared:
            if domain_id not in domains:
                issues.append(_issue("skill_domain_unknown", "role.skills_matrix",
                                     f"{skill_id}: unknown domain {domain_id!r}"))
        if skill_id in BRANCH_SKILLS and not declared:
            issues.append(_issue("skill_domains_empty", "role.skills_matrix",
                                 f"{skill_id}: a branch skill must list its domains"))
        if skill_id not in BRANCH_SKILLS and declared:
            issues.append(_issue("skill_domains_extra", "role.skills_matrix",
                                 f"{skill_id}: a cross-cutting skill must not own domains"))

    if sorted(seen) != sorted(SKILL_IDS):
        issues.append(_issue("skill_set", "role.skills_matrix",
                             f"skills must be exactly {list(SKILL_IDS)}"))
    covered: list[str] = []
    for skill_id in BRANCH_SKILLS:
        covered.extend(_as_list((seen.get(skill_id) or {}).get("domains")))
    if len(covered) != len(set(covered)):
        issues.append(_issue("skill_domain_duplicate", "role.skills_matrix",
                             "a domain is assigned to more than one branch skill"))
    if set(covered) != set(domains):
        missing = sorted(set(domains) - set(covered))
        extra = sorted(set(covered) - set(domains))
        issues.append(_issue("skill_coverage", "role.skills_matrix",
                             f"branch skills must cover every domain; missing={missing} extra={extra}"))
    return issues


def _check_domains(org: dict, org_dir: Path) -> list[dict]:
    issues: list[dict] = []
    domains = org.get("domains")
    if not isinstance(domains, list) or not domains:
        return [_issue("domains", "domains", "domains must be a non-empty list")]
    forums = ((org.get("citation") or {}).get("forums") or {})
    seen: dict[str, dict] = {}
    groups: dict[str, list[str]] = {group: [] for group in GROUPS}
    for index, entry in enumerate(domains):
        if not isinstance(entry, dict):
            issues.append(_issue("domain_type", f"domains[{index}]", "domain must be an object"))
            continue
        domain_id = _text(entry.get("id"))
        if not domain_id:
            issues.append(_issue("domain_id", "domains", "domain id is required"))
            continue
        if domain_id in seen:
            issues.append(_issue("domain_duplicate", "domains", f"duplicate domain {domain_id!r}"))
        seen[domain_id] = entry
        group = _text(entry.get("group"))
        if group not in GROUPS:
            issues.append(_issue("domain_group", "domains", f"{domain_id}: group must be in {list(GROUPS)}"))
            continue
        groups[group].append(domain_id)
        if not _text(entry.get("title")):
            issues.append(_issue("domain_title", "domains", f"{domain_id}: title is required"))
        questions = _as_list(entry.get("questions"))
        if len(questions) < 2:
            issues.append(_issue("domain_questions", "domains",
                                 f"{domain_id}: at least 2 questions are required"))
        for forum_id in _as_list(entry.get("forums")):
            if forum_id not in forums:
                issues.append(_issue("domain_forum", "domains",
                                     f"{domain_id}: unknown forum {forum_id!r}"))
        if not _as_list(entry.get("forums")):
            issues.append(_issue("domain_forums_empty", "domains", f"{domain_id}: forums required"))
        if not _text(entry.get("limits")):
            issues.append(_issue("domain_limits", "domains", f"{domain_id}: limits are required"))

        playbook = PLAYBOOKS[group]
        path = org_dir / "playbooks" / playbook
        if not path.is_file():
            issues.append(_issue("playbook_missing", "playbooks", f"playbook not found: {playbook}"))
        else:
            text = path.read_text(encoding="utf-8")
            if domain_id not in text:
                issues.append(_issue("domain_uncovered", "playbooks",
                                     f"{playbook}: domain {domain_id!r} is not covered"))

    if len(groups["public"]) < MIN_DOMAINS_PUBLIC:
        issues.append(_issue("domains_public", "domains",
                             f"expected at least {MIN_DOMAINS_PUBLIC} public domains, "
                             f"got {len(groups['public'])}"))
    if len(groups["private"]) < MIN_DOMAINS_PRIVATE:
        issues.append(_issue("domains_private", "domains",
                             f"expected at least {MIN_DOMAINS_PRIVATE} private domains, "
                             f"got {len(groups['private'])}"))
    return issues


def _check_source_hierarchy(org: dict) -> list[dict]:
    issues: list[dict] = []
    hierarchy = org.get("source_hierarchy")
    if not isinstance(hierarchy, dict):
        return [_issue("hierarchy", "source_hierarchy", "source_hierarchy block is required")]
    levels = hierarchy.get("levels")
    if not isinstance(levels, list) or tuple(_text(item.get("id")) for item in levels
                                             if isinstance(item, dict)) != LEVELS:
        issues.append(_issue("hierarchy_levels", "source_hierarchy.levels",
                             f"levels must be exactly {list(LEVELS)} in order"))
        levels = [item for item in levels or [] if isinstance(item, dict)]
    if not _text(hierarchy.get("rule")):
        issues.append(_issue("hierarchy_rule", "source_hierarchy.rule", "rule is required"))
    forbidden = _as_list(hierarchy.get("forbidden_as_sole_basis"))
    if len(forbidden) < 3:
        issues.append(_issue("hierarchy_forbidden", "source_hierarchy.forbidden_as_sole_basis",
                             "at least 3 classes must be forbidden as a sole basis"))
    for level in levels:
        level_id = _text(level.get("id"))
        if not _text(level.get("title")):
            issues.append(_issue("level_title", "source_hierarchy.levels", f"{level_id}: title required"))
        examples = _as_list(level.get("examples"))
        if len(examples) < 2:
            issues.append(_issue("level_examples", "source_hierarchy.levels",
                                 f"{level_id}: at least 2 examples are required"))
        ceiling = _text(level.get("evidence_ceiling"))
        if ceiling not in STATUSES:
            issues.append(_issue("level_ceiling", "source_hierarchy.levels",
                                 f"{level_id}: evidence_ceiling must be one of {list(STATUSES)}"))
        if not isinstance(level.get("citation_required"), bool):
            issues.append(_issue("level_citation", "source_hierarchy.levels",
                                 f"{level_id}: citation_required must be a boolean"))
        if not _text(level.get("rule")):
            issues.append(_issue("level_rule", "source_hierarchy.levels", f"{level_id}: rule required"))
    if levels:
        first, last = levels[0], levels[-1]
        if _text(first.get("evidence_ceiling")) != "VERIFIED":
            issues.append(_issue("level_l1_ceiling", "source_hierarchy.levels",
                                 "L1 must allow VERIFIED"))
        if _text(last.get("evidence_ceiling")) != "NOT_FETCHED" or last.get("citation_required") is not False:
            issues.append(_issue("level_l5", "source_hierarchy.levels",
                                 "L5 must be NOT_FETCHED and must not be citable"))
    return issues


def _check_citation_policy(org: dict) -> list[dict]:
    issues: list[dict] = []
    citation = org.get("citation")
    if not isinstance(citation, dict):
        return [_issue("citation", "citation", "citation block is required")]
    if _text(citation.get("schema")) != CITATION_SCHEMA:
        issues.append(_issue("citation_schema", "citation.schema",
                             f"schema must be {CITATION_SCHEMA!r}"))
    forums = citation.get("forums")
    if not isinstance(forums, dict) or len(forums) < MIN_CITATION_FORUMS:
        issues.append(_issue("citation_forums", "citation.forums",
                             f"at least {MIN_CITATION_FORUMS} forums are required"))
        forums = {}
    official = _as_list(citation.get("official_hosts"))
    if len(official) < MIN_OFFICIAL_HOSTS:
        issues.append(_issue("citation_official_hosts", "citation.official_hosts",
                             f"at least {MIN_OFFICIAL_HOSTS} official hosts are required"))
    forbidden = _as_list(citation.get("forbidden_hosts"))
    if len(forbidden) < MIN_FORBIDDEN_HOSTS:
        issues.append(_issue("citation_forbidden_hosts", "citation.forbidden_hosts",
                             f"at least {MIN_FORBIDDEN_HOSTS} forbidden hosts are required"))
    for forum_id, forum in sorted(forums.items()):
        if not isinstance(forum, dict):
            issues.append(_issue("forum_type", "citation.forums", f"{forum_id}: must be an object"))
            continue
        if not _text(forum.get("title")):
            issues.append(_issue("forum_title", "citation.forums", f"{forum_id}: title required"))
        if _text(forum.get("kind")) not in ("court", "database", "legislation"):
            issues.append(_issue("forum_kind", "citation.forums",
                                 f"{forum_id}: kind must be court/database/legislation"))
        hosts = _as_list(forum.get("hosts"))
        if not hosts:
            issues.append(_issue("forum_hosts", "citation.forums", f"{forum_id}: hosts required"))
        for host in hosts:
            if not host_matches(_text(host).lstrip("*."), official):
                issues.append(_issue("forum_host_not_official", "citation.forums",
                                     f"{forum_id}: host {host!r} is not in official_hosts"))

    required = _as_list(citation.get("required_fields"))
    for field in ("forum", "jurisdiction", "title", "date", "numbers", "pinpoint", "holding_type",
                  "good_law", "status"):
        if field not in required:
            issues.append(_issue("citation_required", "citation.required_fields",
                                 f"{field!r} must be a required citation field"))
    if tuple(_as_list(citation.get("statuses"))) != STATUSES:
        issues.append(_issue("citation_statuses", "citation.statuses",
                             f"statuses must be {list(STATUSES)}"))
    if citation.get("status_rank") != STATUS_RANK:
        issues.append(_issue("citation_rank", "citation.status_rank",
                             "status_rank must order NOT_FETCHED < NOT_VERIFIED < PARTIAL < VERIFIED"))
    if tuple(_as_list(citation.get("holding_types"))) != HOLDING_TYPES:
        issues.append(_issue("citation_holding", "citation.holding_types",
                             f"holding_types must be {list(HOLDING_TYPES)}"))
    if tuple(_as_list(citation.get("good_law"))) != GOOD_LAW:
        issues.append(_issue("citation_good_law", "citation.good_law",
                             f"good_law values must be {list(GOOD_LAW)}"))
    if len(_as_list(citation.get("verified_requires"))) < 4:
        issues.append(_issue("citation_verified", "citation.verified_requires",
                             "verified_requires must list at least 4 conditions"))
    if tuple(_as_list(citation.get("verified_by_prefixes"))) != ("human:", "official:"):
        issues.append(_issue("citation_verifier", "citation.verified_by_prefixes",
                             "verified_by must be limited to human:* or official:*"))
    if citation.get("ai_verification_forbidden") is not True:
        issues.append(_issue("citation_ai", "citation.ai_verification_forbidden",
                             "ai_verification_forbidden must be true"))
    if len(_as_list(citation.get("fake_markers"))) < MIN_FAKE_MARKERS:
        issues.append(_issue("citation_fake_markers", "citation.fake_markers",
                             f"at least {MIN_FAKE_MARKERS} fake markers are required"))
    if not _text(citation.get("no_fake_citation")):
        issues.append(_issue("citation_no_fake", "citation.no_fake_citation",
                             "no_fake_citation rule text is required"))
    return issues


def _check_opinion(org: dict) -> list[dict]:
    issues: list[dict] = []
    opinion = org.get("opinion")
    if not isinstance(opinion, dict):
        return [_issue("opinion", "opinion", "opinion block is required")]
    if tuple(_as_list(opinion.get("sections"))) != OPINION_SECTIONS:
        issues.append(_issue("opinion_sections", "opinion.sections",
                             f"sections must be exactly {list(OPINION_SECTIONS)} in order"))
    titles = opinion.get("titles")
    requirements = opinion.get("requirements")
    for section in OPINION_SECTIONS:
        if not isinstance(titles, dict) or not _text(titles.get(section)):
            issues.append(_issue("opinion_title", "opinion.titles", f"{section}: title required"))
        if not isinstance(requirements, dict) or not _text(requirements.get(section)):
            issues.append(_issue("opinion_requirement", "opinion.requirements",
                                 f"{section}: content requirement required"))
    if opinion.get("disclaimer_required") is not True:
        issues.append(_issue("opinion_disclaimer", "opinion.disclaimer_required",
                             "disclaimer_required must be true"))
    if _text(opinion.get("disclaimer")) and DISCLAIMER_ANCHOR not in _text(opinion.get("disclaimer")):
        issues.append(_issue("opinion_disclaimer_text", "opinion.disclaimer",
                             f"the disclaimer must state '{DISCLAIMER_ANCHOR}'"))
    elif not _text(opinion.get("disclaimer")):
        issues.append(_issue("opinion_disclaimer_text", "opinion.disclaimer",
                             "the disclaimer text is required"))
    if opinion.get("client_advice") is not False:
        issues.append(_issue("opinion_client_advice", "opinion.client_advice",
                             "client_advice must be false: the role does not advise clients"))
    if opinion.get("human_review_required") is not True:
        issues.append(_issue("opinion_human_review", "opinion.human_review_required",
                             "human_review_required must be true"))
    return issues


def _check_jurisdictions(org: dict) -> list[dict]:
    issues: list[dict] = []
    jurisdictions = org.get("jurisdictions")
    forums = ((org.get("citation") or {}).get("forums") or {})
    if not isinstance(jurisdictions, list) or not jurisdictions:
        return [_issue("jurisdictions", "jurisdictions",
                       "jurisdictions must be a non-empty list")]
    seen: set[str] = set()
    for entry in jurisdictions:
        if not isinstance(entry, dict):
            issues.append(_issue("jurisdiction_type", "jurisdictions",
                                 "jurisdiction must be an object"))
            continue
        jurisdiction_id = _text(entry.get("id"))
        if not jurisdiction_id:
            issues.append(_issue("jurisdiction_id", "jurisdictions", "id is required"))
            continue
        if not ROLE_ID_RE.match(jurisdiction_id):
            issues.append(_issue("jurisdiction_id_format", "jurisdictions",
                                 f"{jurisdiction_id}: id must be kebab-case"))
        if jurisdiction_id in seen:
            issues.append(_issue("jurisdiction_duplicate", "jurisdictions",
                                 f"duplicate jurisdiction {jurisdiction_id!r}"))
        seen.add(jurisdiction_id)
        if not _text(entry.get("title")):
            issues.append(_issue("jurisdiction_title", "jurisdictions",
                                 f"{jurisdiction_id}: title required"))
        entry_forums = _as_list(entry.get("forums"))
        if not entry_forums:
            issues.append(_issue("jurisdiction_forums", "jurisdictions",
                                 f"{jurisdiction_id}: at least one forum is required"))
        for forum_id in entry_forums:
            if forum_id not in forums:
                issues.append(_issue("jurisdiction_forum", "jurisdictions",
                                     f"{jurisdiction_id}: unknown forum {forum_id!r}"))
        if not _text(entry.get("note")):
            issues.append(_issue("jurisdiction_note", "jurisdictions",
                                 f"{jurisdiction_id}: note required"))
    if DEFAULT_JURISDICTION not in seen:
        issues.append(_issue("jurisdiction_default", "jurisdictions",
                             f"default jurisdiction {DEFAULT_JURISDICTION!r} is missing"))
    return issues


def _check_boundaries(org: dict) -> list[dict]:
    issues: list[dict] = []
    boundaries = org.get("boundaries")
    if not isinstance(boundaries, dict):
        return [_issue("boundaries", "boundaries", "boundaries block is required")]
    for key in REQUIRED_BOUNDARIES:
        value = boundaries.get(key)
        if value is None:
            issues.append(_issue("boundary_missing", f"boundaries.{key}", "boundary rule is required"))
        elif not isinstance(value, str) or not value.strip():
            issues.append(_issue("boundary_empty", f"boundaries.{key}",
                                 "boundary rule must be a non-empty string"))
    checks = {
        "no_legal_advice": "не консультация",
        "no_personal_data": "персональные данные",
        "sanctions": "обход",
        "no_signing": "не подписывает",
        "no_fake_citations": "не выдумываются",
    }
    for key, marker in checks.items():
        if isinstance(boundaries.get(key), str) and marker not in boundaries[key]:
            issues.append(_issue("boundary_text", f"boundaries.{key}",
                                 f"boundary must mention {marker!r}"))
    return issues


def _check_handoffs(org: dict, known_roles: set[str], role_id: str) -> list[dict]:
    issues: list[dict] = []
    handoffs = org.get("handoffs")
    if not isinstance(handoffs, dict):
        return [_issue("handoffs", "handoffs", "handoffs block is required")]
    for key in REQUIRED_HANDOFFS:
        entry = handoffs.get(key)
        if not isinstance(entry, dict):
            issues.append(_issue("handoff_missing", f"handoffs.{key}", "handoff is required"))
            continue
        target = _text(entry.get("role"))
        if not target:
            issues.append(_issue("handoff_role", f"handoffs.{key}", "role is required"))
        elif not _text(entry.get("what")):
            issues.append(_issue("handoff_what", f"handoffs.{key}", "what is required"))
        if target and known_roles and target not in known_roles and target != "human-lawyer":
            issues.append(_issue("handoff_unknown", f"handoffs.{key}",
                                 f"role {target!r} is not known to the AMFE contour"))
        if target == role_id:
            issues.append(_issue("handoff_self", f"handoffs.{key}", "a role cannot hand off to itself"))
    return issues


def check_package(org: dict, org_dir: Path) -> list[dict]:
    """Structural checks of the legal package."""
    issues: list[dict] = []
    if _text(org.get("schema")) != ORG_SCHEMA:
        issues.append(_issue("schema", "schema", f"schema must be {ORG_SCHEMA!r}"))
    if not VERSION_RE.match(_text(org.get("version"))):
        issues.append(_issue("version", "version", "version must look like 1.0.0"))
    if _text(org.get("status")) != "RESEARCH_ONLY":
        issues.append(_issue("status", "status", "status must stay RESEARCH_ONLY"))
    if org.get("admission") is not False:
        issues.append(_issue("admission", "admission", "admission must be false (fail-closed)"))
    if org.get("advice") is not False:
        issues.append(_issue("advice", "advice", "advice must be false: no legal advice automation"))
    as_of = _text(org.get("as_of"))
    if not is_iso_date(as_of):
        issues.append(_issue("as_of", "as_of", "as_of must be an ISO date (YYYY-MM-DD)"))
    if not _text(org.get("purpose", "")) and not _text(org.get("note")):
        issues.append(_issue("purpose", "note", "a purpose/note is required"))

    role = org.get("role") if isinstance(org.get("role"), dict) else {}
    role_id = _text(role.get("id"))
    known_roles, _sources = load_known_roles()
    issues += _check_role(org, org_dir, known_roles)

    domains_list = [entry for entry in _as_list(org.get("domains")) if isinstance(entry, dict)]
    domains = {_text(entry.get("id")): entry for entry in domains_list if _text(entry.get("id"))}
    issues += _check_domains(org, org_dir)
    issues += _check_skills_matrix(org, domains)

    process = org.get("process")
    if not isinstance(process, dict):
        issues.append(_issue("process", "process", "process block is required"))
    else:
        if tuple(_as_list(process.get("order"))) != PROCESS_ORDER:
            issues.append(_issue("process_order", "process.order",
                                 f"process.order must be {list(PROCESS_ORDER)}"))
        owners = process.get("owners") if isinstance(process.get("owners"), dict) else {}
        for step, owner in PROCESS_OWNERS.items():
            if owners.get(step) != owner:
                issues.append(_issue("process_owner", f"process.owners.{step}",
                                     f"owner must be {owner!r}, got {owners.get(step)!r}"))
        if not _text(process.get("exit_rule")):
            issues.append(_issue("process_exit", "process.exit_rule", "exit_rule is required"))

    issues += _check_source_hierarchy(org)
    issues += _check_citation_policy(org)
    issues += _check_opinion(org)
    issues += _check_jurisdictions(org)
    issues += _check_boundaries(org)
    issues += _check_handoffs(org, known_roles, role_id)

    if not isinstance(org.get("non_goals"), list) or len(org.get("non_goals") or []) < 3:
        issues.append(_issue("non_goals", "non_goals", "non_goals must list at least 3 items"))
    if not isinstance(org.get("review_gates"), list) or len(org.get("review_gates") or []) < 4:
        issues.append(_issue("review_gates", "review_gates", "at least 4 review gates are required"))
    if not isinstance(org.get("kpi"), list) or len(org.get("kpi") or []) < 3:
        issues.append(_issue("kpi", "kpi", "kpi must list at least 3 items"))
    if not isinstance(org.get("limits"), list) or len(org.get("limits") or []) < 3:
        issues.append(_issue("limits", "limits", "limits must list at least 3 items"))

    for key, name in PLAYBOOKS.items():
        path = org_dir / "playbooks" / name
        if not path.is_file():
            issues.append(_issue("playbook_missing", "playbooks", f"playbook not found: {name}"))
            continue
        text = path.read_text(encoding="utf-8")
        if len(text) < 1500:
            issues.append(_issue("playbook_short", "playbooks", f"playbook too short: {name}"))
        lowered = text.lower()
        for marker in PLAYBOOK_MARKERS[key]:
            if marker.lower() not in lowered:
                issues.append(_issue("playbook_marker", "playbooks",
                                     f"{name}: missing marker {marker!r}"))
    return issues


# --------------------------------------------------------------------------------------------
# Citation checks
# --------------------------------------------------------------------------------------------

def _citation_numbers(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(_text(item) for item in value if _text(item))
    return ""


def _fake_marker_hit(row: dict, markers) -> str | None:
    haystack = " ".join([
        _text(row.get("title")), _citation_numbers(row.get("numbers")),
        _text(row.get("pinpoint")), _text(row.get("note")), _text(row.get("url")),
    ]).lower()
    for marker in markers:
        if _text(marker).lower() and _text(marker).lower() in haystack:
            return _text(marker)
    return None


def check_citations(rows: list, org: dict) -> list[dict]:
    """Fail-closed validation of a citation list: no missing fields, no unverifiable 'VERIFIED'."""
    issues: list[dict] = []
    citation = org.get("citation") if isinstance(org.get("citation"), dict) else {}
    forums = citation.get("forums") if isinstance(citation.get("forums"), dict) else {}
    official = _as_list(citation.get("official_hosts"))
    forbidden = _as_list(citation.get("forbidden_hosts"))
    fake_markers = _as_list(citation.get("fake_markers"))
    prefixes = tuple(_as_list(citation.get("verified_by_prefixes")))
    as_of = _text(org.get("as_of"))
    min_date = _text((_as_list(citation.get("date_range")) or ["1900-01-01"])[0]) or "1900-01-01"
    jurisdictions = {_text(entry.get("id"))
                     for entry in _as_list(org.get("jurisdictions")) if isinstance(entry, dict)}

    if not isinstance(rows, list) or not rows:
        return [_issue("citations_empty", "citations",
                       "a citations file must contain at least one citation")]
    seen: set[str] = set()
    for index, row in enumerate(rows):
        label = f"citations[{index}]"
        if not isinstance(row, dict):
            issues.append(_issue("citation_type", label, "citation must be an object"))
            continue
        citation_id = _text(row.get("id"))
        if not citation_id:
            issues.append(_issue("citation_id", label, "citation id is required"))
            continue
        label = f"{citation_id}"
        if not CITATION_ID_RE.match(citation_id):
            issues.append(_issue("citation_id_format", label, "id must look like C-01"))
        if citation_id in seen:
            issues.append(_issue("citation_duplicate", label, "duplicate citation id"))
        seen.add(citation_id)

        for field in citation.get("required_fields") or ("forum", "jurisdiction", "title", "date",
                                                         "numbers", "pinpoint", "holding_type",
                                                         "good_law", "status"):
            value = row.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(_issue("citation_required", label, f"missing required field {field!r}"))

        forum_id = _text(row.get("forum"))
        forum = forums.get(forum_id) if forum_id else None
        if forum_id and forum is None:
            issues.append(_issue("citation_forum", label, f"unknown forum {forum_id!r}"))
        jurisdiction_id = _text(row.get("jurisdiction"))
        if jurisdiction_id and jurisdiction_id not in jurisdictions:
            issues.append(_issue("citation_jurisdiction", label,
                                 f"unknown jurisdiction {jurisdiction_id!r}"))

        title = _text(row.get("title"))
        if len(title) < 3:
            issues.append(_issue("citation_title", label, "title must name the case/instrument"))

        date = _text(row.get("date"))
        if date:
            if not is_iso_date(date):
                issues.append(_issue("citation_date", label, "date must be an ISO date (YYYY-MM-DD)"))
            elif as_of and date > as_of:
                issues.append(_issue("citation_date_future", label,
                                     f"date {date} is after as_of {as_of}"))
            elif date < min_date:
                issues.append(_issue("citation_date_old", label, f"date {date} is before {min_date}"))

        numbers = _citation_numbers(row.get("numbers"))
        if not numbers:
            issues.append(_issue("citation_numbers", label,
                                 "numbers (docket/report) must be a non-empty string or list"))

        pinpoint = _text(row.get("pinpoint"))
        if pinpoint and not re.search(r"\d", pinpoint):
            issues.append(_issue("citation_pinpoint", label,
                                 "pinpoint must identify a paragraph/page (contain a digit)"))

        holding = _text(row.get("holding_type"))
        if holding and holding not in HOLDING_TYPES:
            issues.append(_issue("citation_holding", label,
                                 f"holding_type must be one of {list(HOLDING_TYPES)}"))
        good_law = _text(row.get("good_law"))
        if good_law and good_law not in GOOD_LAW:
            issues.append(_issue("citation_good_law", label,
                                 f"good_law must be one of {list(GOOD_LAW)}"))
        status = _text(row.get("status"))
        if status and status not in STATUSES:
            issues.append(_issue("citation_status", label, f"status must be one of {list(STATUSES)}"))

        marker = _fake_marker_hit(row, fake_markers)
        if marker:
            issues.append(_issue("citation_fake_marker", label,
                                 f"placeholder marker {marker!r} found: not a real citation"))

        url = _text(row.get("url"))
        host = host_of(url) if url else None
        if url and host is None:
            issues.append(_issue("citation_url", label, "url must be an absolute http(s) URL"))
        if host and host_matches(host, forbidden):
            issues.append(_issue("citation_forbidden_host", label,
                                 f"host {host!r} is forbidden as a citation locator"))

        if status == "VERIFIED":
            if not url:
                issues.append(_issue("citation_locator", label,
                                     "VERIFIED requires an official locator URL"))
            elif not url.startswith("https://"):
                issues.append(_issue("citation_https", label, "VERIFIED requires an https URL"))
            elif host and forum is not None:
                forum_hosts = _as_list(forum.get("hosts"))
                if not (host_matches(host, forum_hosts) or host_matches(host, official)):
                    issues.append(_issue("citation_host", label,
                                         f"host {host!r} is not an official host for {forum_id!r}"))
            elif host and not host_matches(host, official):
                issues.append(_issue("citation_host", label,
                                     f"host {host!r} is not an official host"))
            retrieved = _text(row.get("retrieved_at"))
            if not retrieved:
                issues.append(_issue("citation_retrieved", label,
                                     "VERIFIED requires retrieved_at (date of retrieval)"))
            elif not is_iso_date(retrieved):
                issues.append(_issue("citation_retrieved_format", label,
                                     "retrieved_at must be an ISO date"))
            elif as_of and retrieved > as_of:
                issues.append(_issue("citation_retrieved_future", label,
                                     f"retrieved_at {retrieved} is after as_of {as_of}"))
            verifier = _text(row.get("verified_by"))
            if not verifier:
                issues.append(_issue("citation_verifier", label,
                                     "VERIFIED requires verified_by = human:* or official:*"))
            else:
                if prefixes and not verifier.startswith(prefixes):
                    issues.append(_issue("citation_verifier_prefix", label,
                                         f"verified_by must start with one of {list(prefixes)}"))
                if AI_VERIFIER_RE.search(verifier):
                    issues.append(_issue("citation_ai_verifier", label,
                                         "a model cannot verify a citation; use human:* or official:*"))
            if good_law == "UNKNOWN":
                issues.append(_issue("citation_good_law_unknown", label,
                                     "VERIFIED requires a known good_law status"))
            if holding == "not_applicable":
                issues.append(_issue("citation_holding_na", label,
                                     "VERIFIED case authority needs ratio/obiter, not not_applicable"))
        if status in ("NOT_VERIFIED", "NOT_FETCHED") and not _text(row.get("note")):
            issues.append(_issue("citation_note", label,
                                 f"{status} requires a note explaining what is missing"))
    return issues


# --------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------

def check(org_path: Path = DEFAULT_ORG,
          citations_path: Path | None = None) -> dict:
    """Run every check and return the machine summary (raises :class:`LegalError`)."""
    org_path = Path(org_path)
    org = load_org(org_path)
    org_dir = org_path.parent
    role = org.get("role") if isinstance(org.get("role"), dict) else {}
    issues = check_package(org, org_dir)
    citation_stats: dict | None = None
    if citations_path is not None:
        try:
            rows = load_citations(Path(citations_path))
        except LegalError as error:
            issues.append(_issue("citation_file", "citations", str(error)))
        else:
            issues += check_citations(rows, org)
            by_status = {status: 0 for status in STATUSES}
            for row in rows:
                if isinstance(row, dict):
                    status = _text(row.get("status"))
                    if status in by_status:
                        by_status[status] += 1
            citation_stats = {"file": str(citations_path), "count": len(rows),
                              "by_status": by_status}
    issues.sort(key=lambda item: (item["code"], item["field"], item["message"]))

    domains = [entry for entry in _as_list(org.get("domains")) if isinstance(entry, dict)]
    counts = {
        "domains": len(domains),
        "domains_public": sum(1 for entry in domains if entry.get("group") == "public"),
        "domains_private": sum(1 for entry in domains if entry.get("group") == "private"),
        "playbooks": len(PLAYBOOKS),
        "forums": len(((org.get("citation") or {}).get("forums") or {})),
        "jurisdictions": len(_as_list(org.get("jurisdictions"))),
        "source_levels": len(_as_list((org.get("source_hierarchy") or {}).get("levels"))),
    }
    return {
        "schema": CHECK_SCHEMA,
        "org": str(org_path),
        "org_version": org.get("version"),
        "org_status": org.get("status"),
        "as_of": org.get("as_of"),
        "role": {
            "id": _text(role.get("id")),
            "alias": _text(role.get("alias")),
            "title": _text(role.get("title")),
            "review_role": _text(role.get("review_role")),
            "human_reviewer": _text(role.get("human_reviewer")),
            "evidence_ceiling": _text(role.get("evidence_ceiling")),
            "advice_ceiling": _text(role.get("advice_ceiling")),
        },
        "valid": not issues,
        "errors": len(issues),
        "counts": counts,
        "source_hierarchy": list(LEVELS),
        "citation_policy": {
            "statuses": list(STATUSES),
            "holding_types": list(HOLDING_TYPES),
            "good_law": list(GOOD_LAW),
            "verified_by": ["human:*", "official:*"],
            "ai_verification": "forbidden",
            "forbidden_hosts": list(_as_list((org.get("citation") or {}).get("forbidden_hosts"))),
            "fake_markers": list(_as_list((org.get("citation") or {}).get("fake_markers"))),
        },
        "opinion_sections": list(_as_list((org.get("opinion") or {}).get("sections"))),
        "boundaries": sorted((org.get("boundaries") or {}).keys()),
        "review_gates": list(_as_list(org.get("review_gates"))),
        "citations": citation_stats,
        "issues": issues,
        "policy": {
            "network": "disabled",
            "clock": "none (relative to as_of)",
            "writes": "none (only --out writes the requested brief)",
            "advice": False,
            "attorney_client": False,
            "personal_data": "forbidden",
            "model_status": "RESEARCH_ONLY",
            "admission": False,
        },
    }


def render_summary(summary: dict) -> str:
    """Human-readable package summary."""
    counts = summary["counts"]
    lines = [
        f"Legal Counsel: `{summary['role']['id']}` (alias `{summary['role']['alias']}`), "
        f"схема {summary['org_version']}, статус {summary['org_status']}, "
        f"advice=false, as_of {summary['as_of']}",
        f"Домены: {counts['domains']} (публичных {counts['domains_public']}, "
        f"частных {counts['domains_private']}); плейбуков: {counts['playbooks']}",
        f"Источники: {' / '.join(summary['source_hierarchy'])}; форумов: {counts['forums']}; "
        f"юрисдикций: {counts['jurisdictions']}",
        f"Заключение: {' → '.join(summary['opinion_sections'])}",
        f"Проверка ссылок: {', '.join(summary['citation_policy']['verified_by'])}; "
        "модель ссылки не подтверждает",
        "",
        "Ворота: " + ", ".join(summary["review_gates"]),
        "Границы: " + ", ".join(summary["boundaries"]),
    ]
    if summary.get("citations"):
        stats = summary["citations"]
        lines += ["", f"Ссылки: {stats['count']} в {stats['file']}; по статусам: {stats['by_status']}"]
    if summary["issues"]:
        lines += ["", f"Ошибки ({summary['errors']}):"]
        for issue in summary["issues"]:
            lines.append(f"  {issue['code']}: {issue['field']}: {issue['message']}")
    else:
        lines += ["", "Проверка пройдена: роль, домены, иерархия, цитаты, формат, границы."]
    lines += ["", "Дальше: python tools/legal_research.py --source-plan --jurisdiction <id>"]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Source plan
# --------------------------------------------------------------------------------------------

def _jurisdiction(org: dict, jurisdiction_id: str) -> dict:
    jurisdiction_id = _text(jurisdiction_id) or DEFAULT_JURISDICTION
    for entry in _as_list(org.get("jurisdictions")):
        if isinstance(entry, dict) and _text(entry.get("id")) == jurisdiction_id:
            return entry
    known = ", ".join(_text(entry.get("id")) for entry in _as_list(org.get("jurisdictions"))
                      if isinstance(entry, dict))
    raise LegalError(f"unknown jurisdiction {jurisdiction_id!r}; known: {known}")


def _forum_line(org: dict, forum_id: str) -> str:
    forum = ((org.get("citation") or {}).get("forums") or {}).get(forum_id) or {}
    hosts = _as_list(forum.get("hosts"))
    host = _text(hosts[0]) if hosts else ""
    title = _text(forum.get("title"))
    if host.startswith("*."):
        host = host[2:]
    return f"{title} ({host})" if host else title


def source_plan_data(org: dict, jurisdiction_id: str = DEFAULT_JURISDICTION) -> dict:
    """Machine-readable source plan for one jurisdiction (no network, no invented facts)."""
    jurisdiction = _jurisdiction(org, jurisdiction_id)
    forums = _as_list(jurisdiction.get("forums"))
    court_like = [forum_id for forum_id in forums
                  if _text((((org.get("citation") or {}).get("forums") or {})
                            .get(forum_id) or {}).get("kind")) in ("court", "legislation")]
    database_like = [forum_id for forum_id in forums if forum_id not in court_like]
    hierarchy = (org.get("source_hierarchy") or {})
    levels = []
    for level in _as_list(hierarchy.get("levels")):
        if not isinstance(level, dict):
            continue
        level_id = _text(level.get("id"))
        if level_id == "L1":
            look_for = _as_list(level.get("examples"))[:2] + [
                _forum_line(org, forum_id) for forum_id in (court_like or forums)[:4]]
        elif level_id == "L2":
            look_for = _as_list(level.get("examples"))[:2] + [
                _forum_line(org, forum_id) for forum_id in database_like[:3]]
        else:
            look_for = _as_list(level.get("examples"))
        levels.append({
            "id": level_id,
            "title": _text(level.get("title")),
            "look_for": look_for,
            "evidence_ceiling": _text(level.get("evidence_ceiling")),
            "citation_required": bool(level.get("citation_required")),
            "rule": _text(level.get("rule")),
        })
    portals = []
    for forum_id in forums:
        forum = ((org.get("citation") or {}).get("forums") or {}).get(forum_id) or {}
        hosts = _as_list(forum.get("hosts"))
        portals.append({
            "forum": forum_id,
            "title": _text(forum.get("title")),
            "kind": _text(forum.get("kind")),
            "host": _text(hosts[0]) if hosts else "",
        })
    citation = org.get("citation") or {}
    return {
        "schema": PLAN_SCHEMA,
        "jurisdiction": _text(jurisdiction.get("id")),
        "title": _text(jurisdiction.get("title")),
        "note": _text(jurisdiction.get("note")),
        "as_of": org.get("as_of"),
        "network": "disabled",
        "levels": levels,
        "portals": portals,
        "columns": ["#", "вопрос", "класс источника", "портал", "ожидаемый статус",
                    "реквизиты", "retrieved_at", "verified_by"],
        "citation_required": ["forum", "jurisdiction", "title", "date", "numbers", "pinpoint",
                              "holding_type", "good_law", "status", "url", "retrieved_at",
                              "verified_by"],
        "rules": [
            _text(hierarchy.get("rule")),
            _text(citation.get("no_fake_citation")),
            "нет источника — NOT_FETCHED с блокером; нашли, но не подтвердили — NOT_VERIFIED",
            "подтверждает только human:* или official:*; модель не подтверждает ссылку",
        ],
    }


def render_source_plan(plan: dict) -> str:
    lines = [
        f"Source plan: {plan['title']} (`{plan['jurisdiction']}`), as_of {plan['as_of']}, "
        "сеть отключена",
        f"Примечание юрисдикции: {plan['note']}",
        "",
        "| уровень | что ищем | потолок | реквизиты обязательны |",
        "|---|---|---|---|",
    ]
    for level in plan["levels"]:
        look_for = "; ".join(level["look_for"][:6])
        lines.append(f"| {level['id']} | {look_for} | {level['evidence_ceiling']} | "
                     f"{'да' if level['citation_required'] else 'нет (не цитируется)'} |")
    lines += ["", "Порталы юрисдикции:"]
    for portal in plan["portals"]:
        lines.append(f"- `{portal['forum']}` — {portal['title']} — https://{portal['host']}")
    lines += ["", "Правила:"]
    for rule in plan["rules"]:
        if rule:
            lines.append(f"- {rule}")
    lines += [
        "",
        "Заполнить до сбора (никаких данных задним числом):",
        "",
        "| # | вопрос | класс источника | портал | ожидаемый статус | реквизиты | retrieved_at | "
        "verified_by |",
        "|---:|---|---|---|---|---|---|---|",
        f"| 1 | <подвопрос> | L1 | <портал> | NOT_FETCHED | <numbers, date, pinpoint> | "
        "— | — |",
        "",
        "Строка без реквизитов остаётся NOT_VERIFIED: в заключение она не идёт как authority.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Brief
# --------------------------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scan_output(text: str) -> None:
    """Defensive scan: no personal data, no advice claims, no admission claims."""
    if EMAIL_RE.search(text):
        raise LegalError("output would contain an e-mail address; refusing to emit it")
    if PHONE_RE.search(text):
        raise LegalError("output would contain a phone-like number; refusing to emit it")
    if IBAN_RE.search(text):
        raise LegalError("output would contain an IBAN-like value; refusing to emit it")
    if ADVICE_RE.search(text):
        raise LegalError("output would contain an advice/guarantee claim; refusing to emit it")
    if ADMISSION_RE.search(text):
        raise LegalError("output would mention admission; refusing to emit it")
    if DISCLAIMER_ANCHOR not in text:
        raise LegalError("output must carry the 'not legal advice' disclaimer")


def _citation_block(rows: list[dict] | None, citation_policy: dict) -> str:
    if not rows:
        return "\n".join([
            "Ссылки: нет ни одной подтверждённой. Это не ошибка, а состояние по умолчанию:",
            "NOT_VERIFIED для найденного, NOT_FETCHED для ненайденного. Вставлять реквизиты",
            "по памяти запрещено. Заполнить после проверки официального локатора.",
        ])
    lines = [
        "| # | норма/дело | форум | реквизиты | pinpoint | L | статус | ratio/obiter | good_law | URL |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        status = _text(row.get("status"))
        level = "L1" if status == "VERIFIED" else ("L2" if status == "PARTIAL" else "—")
        lines.append(
            f"| {_text(row.get('id'))} | {_text(row.get('title'))} | {_text(row.get('forum'))} | "
            f"{_citation_numbers(row.get('numbers'))} | {_text(row.get('pinpoint'))} | {level} | "
            f"{status} | {_text(row.get('holding_type'))} | {_text(row.get('good_law'))} | "
            f"{_text(row.get('url')) or '—'} |")
    unverified = [row for row in rows if _text(row.get("status")) != "VERIFIED"]
    if unverified:
        lines += ["", f"Непроверенных ссылок: {len(unverified)} — в текст как authority не идут."]
    return "\n".join(lines)


def build_brief(org: dict, role_id: str, question: str, jurisdiction_id: str = DEFAULT_JURISDICTION,
                citations: list[dict] | None = None) -> str:
    """Build a deterministic research-memo brief. No network, no advice, no invented citations."""
    role = org.get("role") if isinstance(org.get("role"), dict) else {}
    canonical = _text(role.get("id"))
    alias = _text(role.get("alias"))
    if _text(role_id) not in (canonical, alias):
        raise LegalError(f"unknown role {role_id!r}; known: {canonical!r} (alias {alias!r})")
    question = (question or "").strip()
    if len(question) < 12:
        raise LegalError("question is too short: need a decision-relevant legal question (>= 12 chars)")
    plan = source_plan_data(org, jurisdiction_id)
    opinion = org.get("opinion") or {}
    citation_policy = org.get("citation") or {}
    sections = _as_list(opinion.get("sections"))
    titles = opinion.get("titles") or {}
    requirements = opinion.get("requirements") or {}
    roles_known, _sources = load_known_roles()

    lines = [
        f"# Legal research brief: {_text(role.get('title'))} (`{canonical}`)",
        "",
        f"Схема: {org.get('schema')} v{org.get('version')} · статус {org.get('status')} · "
        f"advice=false · as_of {org.get('as_of')}",
        "Сеть отключена: tool не выполняет вызовов; реквизиты не подставляются и не выдумываются.",
        "",
        f"**Не является юридической консультацией.** Это исследовательская записка; юридическую",
        "проверку и подпись делает человек-юрист. Клиентское заключение роль не выпускает.",
        "",
        "## Вопрос",
        question,
        "",
        "## Юрисдикция и применимое право",
        f"- Юрисдикция: `{plan['jurisdiction']}` — {plan['title']}",
        f"- Примечание: {plan['note']}",
        "- Применимое право и форум определяются до анализа; если юрисдикция не определена —",
        "  вернуть вопрос `lead`, а не угадывать.",
        "",
        "## Мандат роли",
        _text(role.get("mandate")),
        "",
        "## Source plan (заполнить до сбора)",
        render_source_plan(plan),
        "",
        "## Формат заключения (обязательные разделы)",
    ]
    for section in sections:
        lines.append(f"- **{_text(titles.get(section))}** (`{section}`): "
                     f"{_text(requirements.get(section))}")
    lines += [
        "",
        "## Ссылки",
        _citation_block(citations, citation_policy),
        "",
        "## Правила цитирования (fail-closed)",
        f"- реквизиты: {', '.join(_as_list(citation_policy.get('required_fields')))}",
        f"- статусы: {', '.join(STATUSES)}; подтверждает только "
        f"{' или '.join(_as_list(citation_policy.get('verified_by_prefixes')))}",
        "- `holding_type` отделяет ratio от obiter; `good_law` — статус решения после проверки",
        "- ссылка на энциклопедию, файлообменник или ответ чат-бота не является источником",
        "- нет официального локатора — нет VERIFIED; нет реквизитов — нет ссылки",
        "",
        "## Порядок работы",
        "1. Проверить конфликт интересов и санкционный периметр (до анализа).",
        "2. Заполнить source plan: класс L1–L5, портал, ожидаемый статус, кто проверяет.",
        "3. Собрать нормы и практику только с официальных локаторов; отметить, что именно взято.",
        "4. Проверить каждую ссылку: форум, реквизиты, дата, pinpoint, ratio/obiter, good_law.",
        "5. Собрать семь разделов; альтернативное толкование обязательно; оговорки — содержательные.",
        "6. Передать `protocol-reviewer` и человеку-юристу; до этого никому не отправлять.",
        "",
        "## Критерий приёмки",
        "- каждая ссылка с реквизитами и официальным локатором либо NOT_VERIFIED/NOT_FETCHED;",
        "- ни одного утверждения о норме без редакции/даты и источника L1;",
        "- ratio и obiter разделены; последующая история дела проверена или помечена UNKNOWN;",
        "- все семь разделов на месте; риски и опции содержательны; оговорки не пустые;",
        "- нет персональных данных, обещаний результата и клиентских консультаций;",
        "- ответ относится к заданному вопросу и честно перечисляет непроверенное.",
        "",
        "## Передача",
        f"`{_text(role.get('review_role'))}` — независимая проверка структуры и ссылок; "
        f"`{_text(role.get('human_reviewer'))}` — правовая проверка и подпись; `lead` — решение "
        "о дальнейшем использовании; `kb-librarian` — запись в базу знаний без персональных данных.",
        "",
        "## Запрещено",
    ]
    for item in _as_list(org.get("non_goals"))[:5]:
        lines.append(f"- {item}")
    lines += [
        "- вставлять реквизиты по памяти, из пересказа или из ответа чат-бота;",
        "- подтверждать ссылку самому: только human:* или official:*;",
        "- выдавать obiter за ratio и решение нижестоящего суда за обязательный прецедент;",
        "- советовать обход санкций, экспортного контроля или иных запретов;",
        "- собирать персональные данные и пересылать материалы третьим лицам;",
        "- подписывать, подавать, заверять и обещать исход;",
        "- публиковать, отправлять и коммитить: это делает только Lead.",
        "",
        "## Дисклеймер",
        _text(opinion.get("disclaimer")),
        "",
    ]
    body = "\n".join(lines)
    text = body + (f"_Generated by tools/legal_research.py (no network, no clock, no advice). "
                   f"Brief body hash: {_sha256(body)}_\n")
    _scan_output(text)
    return text


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AMFE legal-counsel package validator / source plan / brief builder")
    parser.add_argument("--org-file", default=str(DEFAULT_ORG), help="path to org.json")
    parser.add_argument("--citations", help="path to a citations JSON file to validate")
    parser.add_argument("--check", action="store_true", help="validate the package; exit 2 on issues")
    parser.add_argument("--summary", action="store_true", help="print the package summary")
    parser.add_argument("--source-plan", action="store_true", help="print the source plan")
    parser.add_argument("--role", default="", help="role id or alias for a research brief")
    parser.add_argument("--question", default="", help="legal question for the brief")
    parser.add_argument("--jurisdiction", default=DEFAULT_JURISDICTION,
                        help="jurisdiction id (default: intl)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--out", help="write the brief/plan to this file instead of stdout")
    args = parser.parse_args(argv)

    org_path = Path(args.org_file)
    citations_path = Path(args.citations) if args.citations else None
    try:
        org = load_org(org_path)
        if args.role:
            rows = load_citations(citations_path) if citations_path else None
            if rows is not None:
                problems = check_citations(rows, org)
                if problems:
                    detail = "; ".join(f"{item['code']}({item['field']}): {item['message']}"
                                       for item in problems[:5])
                    raise LegalError(f"citations rejected ({len(problems)} issue(s)): {detail}")
            brief = build_brief(org, args.role, args.question, args.jurisdiction, rows)
            if args.out:
                out = Path(args.out)
                if not out.parent.exists():
                    raise LegalError(f"output directory does not exist: {out.parent}")
                out.write_text(brief, encoding="utf-8", newline="\n")
                print(f"brief written: {out}")
            elif args.json:
                role = org.get("role") or {}
                print(json.dumps({
                    "schema": BRIEF_SCHEMA,
                    "role": _text(role.get("id")),
                    "alias": _text(role.get("alias")),
                    "question": args.question,
                    "jurisdiction": args.jurisdiction,
                    "as_of": org.get("as_of"),
                    "review_role": _text(role.get("review_role")),
                    "human_reviewer": _text(role.get("human_reviewer")),
                    "advice": False,
                    "citations": len(rows) if rows else 0,
                    "brief": brief,
                    "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
                    "network": "disabled",
                }, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(brief)
            return 0

        if args.source_plan:
            plan = source_plan_data(org, args.jurisdiction)
            if args.out:
                out = Path(args.out)
                if not out.parent.exists():
                    raise LegalError(f"output directory does not exist: {out.parent}")
                out.write_text(render_source_plan(plan), encoding="utf-8", newline="\n")
                print(f"source plan written: {out}")
            elif args.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(render_source_plan(plan))
            return 0

        summary = check(org_path, citations_path)
    except LegalError as error:
        print(f"BLOCKED_INPUT: {error}", file=sys.stderr)
        return 2

    code = 0 if summary["valid"] else 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.summary:
        print(render_summary(summary))
    elif args.check:
        if summary["valid"]:
            counts = summary["counts"]
            print(f"OK: role={summary['role']['id']}, domains={counts['domains']} "
                  f"({counts['domains_public']}+{counts['domains_private']}), "
                  f"playbooks={counts['playbooks']}, forums={counts['forums']}, "
                  f"jurisdictions={counts['jurisdictions']}, as_of={summary['as_of']}, "
                  f"advice=false")
        else:
            print(f"FAIL: {summary['errors']} issue(s)", file=sys.stderr)
            for issue in summary["issues"]:
                print(f"  {issue['code']}: {issue['field']}: {issue['message']}", file=sys.stderr)
    else:
        print(render_summary(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
