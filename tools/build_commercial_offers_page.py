#!/usr/bin/env python3
"""Render static RU/EN page for the 25 fixed-price commercial offer hypotheses."""
from __future__ import annotations
import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "marketing" / "commercial_offers_25" / "offers.json"
BUSINESS = ROOT / "site" / "business"
NAV_RU = [("index.html", "Главная"), ("solutions.html", "Решения"), ("cases.html", "Кейсы"),
          ("cases-100.html", "102 кейса"), ("offers-25.html", "25 офферов"),
          ("pricing.html", "Цены"), ("security.html", "Безопасность"), ("contact.html", "Контакты")]
NAV_EN = [("index.html", "Home"), ("solutions.html", "Solutions"), ("cases.html", "Cases"),
          ("cases-100.html", "102 cases"), ("offers-25.html", "25 offers"),
          ("pricing.html", "Pricing"), ("security.html", "Security"), ("contact.html", "Contact")]


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def render(data: dict, lang: str) -> str:
    en = lang == "en"
    offers = data["offers"]
    nav = NAV_EN if en else NAV_RU
    links = "\n".join(
        f'<a href="{esc(href)}"{(" class=\"on\" aria-current=\"page\"" if href == "offers-25.html" else "")}>{esc(label)}</a>'
        for href, label in nav
    )
    title = "25 Fixed-Price Offers for Institutions and Corporations" if en else "25 офферов с фиксированной ценой для организаций"
    desc = ("AMFE list prices for public-sector and corporate scenarios. Hypotheses, not clients; USD before tax."
            if en else "Фиксированные цены AMFE для сценарной аналитики организаций. Гипотезы, не клиенты; USD до налогов.")
    if en:
        intro = ("This catalogue contains 25 fixed-scope AMFE list-price offers for public institutions, funds, "
                 "banks, utilities, energy companies, traders and associations. Prices are our proposed fees, "
                 "not market benchmarks or claims about a buyer's budget. No offer is agreed, sent or client-specific.")
        disclaimer = ("Organization types and buyer roles only. No private-person targeting, contact scraping, "
                      "hidden influence or outreach by the agent. Licensed third-party data and legal services are excluded.")
        headers = ["ID", "Offer", "Buyer organization / role", "Modules", "Weeks", "Fixed price, USD"]
        section = "Offer scopes"
        cta = "Discuss an offer"
        footer = "RESEARCH_ONLY · admission=false · HYPOTHESIS · not_client=true · not a forecast or investment advice."
        question_label, deliver_label, terms_label, exclusions_label = "Discovery question", "Deliverables", "Assumptions", "Exclusions"
        prices_note = "Taxes excluded; list prices are fixed for stated scope. Changes require a revised quote."
    else:
        intro = ("Каталог содержит 25 офферов AMFE с фиксированным scope и ценой для госструктур, фондов, "
                 "банков, utilities, энергокомпаний, трейдеров и ассоциаций. Это наши цены, а не рыночный бенчмарк "
                 "и не утверждение о бюджете покупателя. Ни один оффер не согласован, не отправлен и не является клиентским.")
        disclaimer = ("Указаны только тип организации и роли покупателя. Нет таргетинга частных лиц, сбора контактов, "
                      "скрытого влияния или автоматической рассылки. Лицензированные сторонние данные и юридические услуги не включены.")
        headers = ["ID", "Оффер", "Организация / роль", "Модули", "Нед.", "Фиксированная цена, USD"]
        section = "Содержание офферов"
        cta = "Обсудить scope"
        footer = "RESEARCH_ONLY · admission=false · HYPOTHESIS · not_client=true · не прогноз и не инвестиционная рекомендация."
        question_label, deliver_label, terms_label, exclusions_label = "Вопрос discovery", "Результат", "Допущения", "Не входит"
        prices_note = "До налогов; цена фиксирована для указанного scope. Изменение scope требует новой сметы."

    rows = []
    for offer in offers:
        title_field = "title_en" if en else "title_ru"
        org_role = f"{offer['buyer_org_type']} / {offer['buyer_role']}"
        rows.append("<tr>" + "".join([
            f"<td><code>{esc(offer['id'])}</code></td>",
            f"<td>{esc(offer[title_field])}</td>",
            f"<td>{esc(org_role)}</td>",
            f"<td>{esc(', '.join(offer['modules']))}</td>",
            f"<td class=\"num\">{offer['duration_weeks']}</td>",
            f"<td class=\"num\">{offer['fixed_price_usd']:,} USD</td>".replace(",", " "),
        ]) + "</tr>")
    details = []
    for offer in offers:
        title_field = "title_en" if en else "title_ru"
        need = offer["need_hypothesis_en" if en else "need_hypothesis_ru"]
        question = offer["question_en" if en else "question_ru"]
        deliverables = offer["deliverables_en" if en else "deliverables_ru"]
        assumptions = offer["assumptions_en" if en else "assumptions_ru"]
        exclusions = offer["exclusions_en" if en else "exclusions_ru"]
        price = f"{offer['fixed_price_usd']:,}".replace(",", " ")
        details.append(f"""<article class="panel offer" id="{esc(offer['id'])}">
<h3>{esc(offer['id'])} · {esc(offer[title_field])}</h3>
<p><span class="badge warn">HYPOTHESIS · not_client</span> <span class="muted">{esc(offer['buyer_org_type'])} / {esc(offer['buyer_role'])}</span></p>
<p><strong>{esc(question_label)}:</strong> {esc(question)}</p>
<p><strong>{esc(deliver_label)}:</strong> {esc(deliverables)}</p>
<p><strong>{esc(terms_label)}:</strong> {esc(' '.join(assumptions))}</p>
<p><strong>{esc(exclusions_label)}:</strong> {esc(' '.join(exclusions))}</p>
<p><strong>USD {price} · {offer['duration_weeks']} {'weeks' if en else 'нед.'}</strong></p>
</article>""")
    lang_switch = '<a href="en/offers-25.html" hreflang="en" lang="en">EN</a>' if not en else '<a href="../offers-25.html" hreflang="ru" lang="ru">RU</a>'
    research = '<a href="../../index.html">Research site</a>' if en else '<a href="../index.html">Исследовательский сайт</a>'
    css = "../assets/style.css" if en else "assets/style.css"
    mail_subject = "AMFE%2025%20offers" if en else "AMFE%2025%20offers"
    table_header = "".join(f"<th>{esc(h)}</th>" for h in headers)
    page = f'''<!doctype html>
<html lang="{lang}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — AMFE</title><meta name="description" content="{esc(desc)}">
<meta name="theme-color" content="#0b3b4a"><link rel="stylesheet" href="{css}">
</head><body>
<header class="top"><div class="wrap top-bar">
<a class="brand" href="index.html">AMFE<span class="brand-sub"> · scenario analytics</span></a>
<p class="meta">{lang_switch} · {research}</p>
<nav class="nav" aria-label="Navigation">{links}</nav>
</div></header>
<main class="wrap">
<section class="hero"><p class="kicker">AMFE · DRAFT · {"2026-11-23"}</p>
<h1>{esc(title)}</h1><p class="lead">{esc(intro)}</p>
<ul class="badges"><li><span class="badge warn">RESEARCH_ONLY</span></li><li><span class="badge warn">admission=false</span></li><li><span class="badge warn">HYPOTHESIS</span></li><li><span class="badge warn">not_client=true</span></li></ul>
<ul class="cta"><li><a class="btn primary" href="mailto:flumos@gmail.com?subject={mail_subject}">{esc(cta)}</a></li><li><a class="btn" href="pricing.html">{"Pricing" if en else "Цены"}</a></li></ul>
</section>
<section class="callout"><h2>{"Commercial and legal boundaries" if en else "Коммерческие и правовые границы"}</h2><p>{esc(disclaimer)}</p><p>{esc(prices_note)} {"No emails have been sent." if en else "Письма не отправлялись."}</p></section>
<section><h2>{esc(section)} · 25</h2><div class="table-wrap"><table><caption>{esc(prices_note)}</caption><thead><tr>{table_header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section><h2>{"Detailed scope (hypotheses)" if en else "Объём каждого оффера (гипотезы)"}</h2>{''.join(details)}</section>
</main><footer class="foot"><div class="wrap"><p><strong>{esc(footer)}</strong></p><p>Contact: <a href="mailto:flumos@gmail.com">flumos@gmail.com</a> · no automated outreach · static page.</p></div></footer>
</body></html>'''
    return page


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    BUSINESS.mkdir(parents=True, exist_ok=True)
    (BUSINESS / "offers-25.html").write_text(render(data, "ru"), encoding="utf-8", newline="\n")
    (BUSINESS / "en").mkdir(parents=True, exist_ok=True)
    (BUSINESS / "en" / "offers-25.html").write_text(render(data, "en"), encoding="utf-8", newline="\n")
    print("built RU/EN 25-offer pages")


if __name__ == "__main__":
    main()
