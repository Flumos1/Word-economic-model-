#!/usr/bin/env python3
"""Offline, fail-closed assistant for lawful B2B/B2G offer qualification.

It prepares role-based briefs only. It does not search for private people, access gated data,
contact prospects, submit tenders, negotiate, or send messages.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORG = ROOT / "agents" / "commercial" / "org.json"
PLAYBOOKS = ROOT / "agents" / "commercial" / "playbooks"
OFFERS = ROOT / "marketing" / "commercial_offers_25" / "offers.json"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{7,}\d)|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
FORBIDDEN = ("covert influence", "hidden network", "secret contact", "scrape personal", "send email", "отправить письмо", "закулисье", "скрытое влияние")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate() -> list[str]:
    errors = []
    try:
        org = load(ORG)
        offers = load(OFFERS)
    except (OSError, json.JSONDecodeError) as exc:
        return [str(exc)]
    role = org.get("role", {})
    if org.get("schema") != "amfe.commercial.org.v1":
        errors.append("bad commercial agent schema")
    if role.get("id") != "commercial-strategist" or role.get("status") != "ACTIVE":
        errors.append("commercial-strategist role missing or inactive")
    if role.get("evidence_ceiling") != "PARTIAL":
        errors.append("evidence_ceiling must remain PARTIAL")
    if role.get("handoffs", {}).get("approval_and_outreach") != "lead/human only":
        errors.append("outreach approval must remain human-only")
    for path in (PLAYBOOKS / "pricing.md", PLAYBOOKS / "procurement.md",
                 PLAYBOOKS / "discovery.md", PLAYBOOKS / "handoffs.md"):
        if not path.is_file() or path.stat().st_size < 150:
            errors.append(f"missing/short playbook: {path.name}")
    if len(offers.get("offers", [])) != 25:
        errors.append("commercial offers source must contain exactly 25 offers")
    return errors


def build_brief(question: str, buyer_type: str, jurisdiction: str, offer_id: str | None = None) -> dict:
    text = f"{question} {buyer_type} {jurisdiction}".strip()
    if len(question.strip()) < 12:
        raise ValueError("question must be at least 12 characters")
    if EMAIL_RE.search(text) or PHONE_RE.search(text):
        raise ValueError("private contacts are not accepted; provide buyer role, not person/contact")
    lower = text.lower()
    if any(term in lower for term in FORBIDDEN):
        raise ValueError("covert targeting/outreach is outside this agent's mandate")
    offers = load(OFFERS)["offers"]
    selected = None
    if offer_id:
        selected = next((o for o in offers if o["id"] == offer_id), None)
        if not selected:
            raise ValueError(f"unknown offer_id: {offer_id}")
    return {
        "schema": "amfe.commercial.brief.v1",
        "role": "commercial-strategist",
        "buyer_type": buyer_type.strip(),
        "buyer_role": "UNVERIFIED_ROLE_ONLY",
        "jurisdiction": jurisdiction.strip().upper(),
        "question": question.strip(),
        "offer": ({"id": selected["id"], "title": selected["title_en"],
                   "fixed_price_usd": selected["fixed_price_usd"],
                   "duration_weeks": selected["duration_weeks"],
                   "evidence_status": selected["evidence_status"],
                   "not_client": selected["not_client"]} if selected else None),
        "required_handoffs": ["osint-scout: public facts/citations", "archivist: context pack",
                              "international-counsel/human-lawyer: legal questions",
                              "lead/human: price and outreach approval"],
        "status": "HYPOTHESIS",
        "sent": False,
        "outreach_authorized": False,
        "disclaimer": "No outreach is sent. Buyer pain and role are hypotheses until human discovery.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--role", default="commercial-strategist")
    parser.add_argument("--question")
    parser.add_argument("--buyer-type")
    parser.add_argument("--jurisdiction", default="MULTI")
    parser.add_argument("--offer-id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.check:
        errors = validate()
        if errors:
            print("FAIL: " + "; ".join(errors), file=sys.stderr)
            return 2
        print("OK: commercial-strategist active; 25 offers; human-only outreach; no network")
        return 0
    if args.role != "commercial-strategist":
        print("unknown role", file=sys.stderr)
        return 2
    if not args.question or not args.buyer_type:
        print("--question and --buyer-type are required", file=sys.stderr)
        return 2
    try:
        brief = build_brief(args.question, args.buyer_type, args.jurisdiction, args.offer_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    output = json.dumps(brief, ensure_ascii=False, indent=1, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n", encoding="utf-8", newline="\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
