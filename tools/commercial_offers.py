#!/usr/bin/env python3
"""Fail-closed validator and deterministic renderer for 25 commercial offer-cases.

These are AMFE list prices for fixed scopes, not market-price benchmarks or buyer budget claims.
The tool performs no network access and sends no outreach.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "marketing" / "commercial_offers_25" / "offers.json"
RU_MD = ROOT / "marketing" / "commercial_offers_25" / "catalog.md"
EN_MD = ROOT / "marketing" / "commercial_offers_25" / "catalog_en.md"
SCHEMA = "amfe.commercial_offers.v1"
COUNT = 25
PRICE_BANDS = (
    (0, 0, 1, 1),
    (7500, 10000, 2, 4),
    (15000, 25000, 4, 6),
    (20000, 30000, 8, 12),
    (25000, 80000, 6, 12),
)
REQUIRED = (
    "id", "segment", "buyer_org_type", "buyer_role", "jurisdiction_scope", "title_ru", "title_en",
    "need_hypothesis_ru", "need_hypothesis_en", "question_ru", "question_en", "deliverables_ru",
    "deliverables_en", "modules", "duration_weeks", "fixed_price_usd", "price_terms_ru",
    "price_terms_en", "assumptions_ru", "assumptions_en", "exclusions_ru", "exclusions_en",
    "evidence_status", "not_client", "sent", "status",
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{7,}\d)|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
URL_RE = re.compile(r"https?://|www\.", re.I)
BANNED = (
    "admitted", "точность выше", "гарантируем", "we guarantee", "guaranteed savings",
    "guaranteed accuracy", "client case", "мы работали", "наш клиент", "our client",
    "secret network", "закулисье", "скрытое влияние",
)
MODULES = {"Ормуз/бюджет", "FLAKS TCO", "газ/СПГ", "геополитика", "API/Client Pack"}


class OfferError(ValueError):
    pass


def load(path: Path = DATA) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)


def validate(document: dict) -> list[str]:
    errors: list[str] = []
    if document.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    offers = document.get("offers")
    if not isinstance(offers, list) or len(offers) != COUNT:
        errors.append(f"offers must contain exactly {COUNT} cases")
        return errors
    if document.get("offers_count") != COUNT:
        errors.append(f"offers_count must be {COUNT}")
    if document.get("currency") != "USD" or document.get("taxes_included") is not False:
        errors.append("currency must be USD and taxes_included must be false")
    seen = set()
    org_types = set()
    for offer in offers:
        oid = str(offer.get("id", "<missing>"))
        if not re.fullmatch(r"C25-\d{3}", oid):
            errors.append(f"{oid}: invalid id")
        if oid in seen:
            errors.append(f"{oid}: duplicate id")
        seen.add(oid)
        missing = [field for field in REQUIRED if field not in offer]
        if missing:
            errors.append(f"{oid}: missing fields {missing}")
            continue
        org_types.add(offer["buyer_org_type"])
        if offer["evidence_status"] != "HYPOTHESIS" or offer["not_client"] is not True \
                or offer["sent"] is not False or offer["status"] != "DRAFT":
            errors.append(f"{oid}: flags must remain HYPOTHESIS/not_client/sent=false/DRAFT")
        if not isinstance(offer["modules"], list) or not offer["modules"] \
                or any(module not in MODULES for module in offer["modules"]):
            errors.append(f"{oid}: modules must be known and non-empty")
        price, weeks = offer["fixed_price_usd"], offer["duration_weeks"]
        if not isinstance(price, int) or isinstance(price, bool) or not isinstance(weeks, int):
            errors.append(f"{oid}: price and duration must be integers")
            continue
        matching = [(low, high, min_w, max_w) for low, high, min_w, max_w in PRICE_BANDS
                    if low <= price <= high and min_w <= weeks <= max_w]
        if not matching:
            errors.append(f"{oid}: price/duration outside rate card")
        for field in ("buyer_role", "title_ru", "title_en", "question_ru", "question_en"):
            text = str(offer[field])
            if EMAIL_RE.search(text) or PHONE_RE.search(text) or URL_RE.search(text):
                errors.append(f"{oid}: contact or URL in {field}")
        joined = " ".join(_texts(offer)).lower()
        for phrase in BANNED:
            if phrase.lower() in joined:
                errors.append(f"{oid}: banned claim/concept {phrase!r}")
    if len(org_types) < 8:
        errors.append("buyer org-type coverage must include at least 8 categories")
    if seen != {f"C25-{n:03d}" for n in range(1, COUNT + 1)}:
        errors.append("IDs must be consecutive C25-001..C25-025")
    return sorted(set(errors))


def render(document: dict, lang: str) -> str:
    offers = document["offers"]
    en = lang == "en"
    title = "25 Priced Institutional Offers" if en else "25 коммерческих офферов для организаций"
    lines = [f"# {title}", "", "**DRAFT · RESEARCH_ONLY · admission=false · HYPOTHESIS · not_client=true · sent=false**", ""]
    if en:
        lines += ["List prices are AMFE prices for the stated scope, USD before tax. They are not market benchmarks or claims about a buyer's budget.",
                  "All buyer centers are organization-level roles; no named individuals, contacts, hidden networks or outreach.",
                  "Third-party licensed data, legal work and client-specific implementation beyond scope are excluded.", "",
                  "| ID | Offer | Buyer organization / role | Modules | Weeks | Fixed USD |",
                  "|---|---|---|---|---:|---:|"]
        for o in offers:
            lines.append(f"| {o['id']} | {o['title_en']} | {o['buyer_org_type']} / {o['buyer_role']} | {', '.join(o['modules'])} | {o['duration_weeks']} | {o['fixed_price_usd']:,} |".replace(",", " "))
        lines += ["", "## Price terms", "", "A fixed list price applies only to the described scope and assumptions; changes require a revised written quote. No savings, forecast accuracy, investment return or admission status is promised.", ""]
        for o in offers:
            lines += [f"### {o['id']} — {o['title_en']}", "", o["need_hypothesis_en"], "", f"**Question:** {o['question_en']}", "",
                      f"**Deliverables:** {o['deliverables_en']}", "",
                      f"**Price / timing:** USD {o['fixed_price_usd']:,} / {o['duration_weeks']} weeks. {o['price_terms_en']}", "",
                      f"**Assumptions:** {' '.join(o['assumptions_en'])}", "",
                      f"**Exclusions:** {' '.join(o['exclusions_en'])}", ""]
    else:
        lines += ["Цены — наши фиксированные list prices для указанного объёма, USD до налогов. Это не рыночные бенчмарки и не утверждения о бюджете покупателя.",
                  "Покупатели указаны как типы организаций и должностные роли; без персональных имён, контактов, скрытых сетей и рассылки.",
                  "Лицензии на сторонние данные, юридическая работа и внедрение сверх scope не входят.", "",
                  "| ID | Оффер | Организация / роль | Модули | Нед. | Фиксированная цена, USD |",
                  "|---|---|---|---|---:|---:|"]
        for o in offers:
            price = f"{o['fixed_price_usd']:,}".replace(",", " ")
            lines.append(f"| {o['id']} | {o['title_ru']} | {o['buyer_org_type']} / {o['buyer_role']} | {', '.join(o['modules'])} | {o['duration_weeks']} | {price} |")
        lines += ["", "## Условия цены", "", "Фиксированная list price действует только для описанного объёма и допущений; изменение scope — отдельная смета. Экономия, точность прогноза, инвестиционный результат и допуск модели не обещаются.", ""]
        for o in offers:
            price = f"{o['fixed_price_usd']:,}".replace(",", " ")
            lines += [f"### {o['id']} — {o['title_ru']}", "", o["need_hypothesis_ru"], "", f"**Вопрос:** {o['question_ru']}", "",
                      f"**Результат:** {o['deliverables_ru']}", "",
                      f"**Цена / срок:** {price} USD / {o['duration_weeks']} нед. {o['price_terms_ru']}", "",
                      f"**Допущения:** {' '.join(o['assumptions_ru'])}", "",
                      f"**Не входит:** {' '.join(o['exclusions_ru'])}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--build-md", action="store_true")
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        doc = load(args.data)
        errors = validate(doc)
        if errors:
            print("FAIL: " + "; ".join(errors), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"ok": True, "offers": COUNT,
                              "buyer_org_types": len({o["buyer_org_type"] for o in doc["offers"]}),
                              "prices_usd": sorted({o["fixed_price_usd"] for o in doc["offers"]})},
                             ensure_ascii=False, sort_keys=True))
            return 0
        if args.build_md:
            out = args.out or args.data.parent
            out.mkdir(parents=True, exist_ok=True)
            (out / "catalog.md").write_text(render(doc, "ru"), encoding="utf-8", newline="\n")
            (out / "catalog_en.md").write_text(render(doc, "en"), encoding="utf-8", newline="\n")
            print(f"OK: built 25 offers to {out}")
            return 0
        print(f"OK: {COUNT} fixed-price offers, {len({o['buyer_org_type'] for o in doc['offers']})} org types, USD")
        return 0
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
