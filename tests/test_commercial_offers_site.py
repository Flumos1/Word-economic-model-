#!/usr/bin/env python3
"""Static bilingual offers page tests: list prices, lawful buyer roles, no outreach claims."""
from __future__ import annotations
import json
import re
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SITE=ROOT/"site"/"business"
DATA=ROOT/"marketing"/"commercial_offers_25"/"offers.json"
FORBIDDEN=("<script", "<form", "<input", "http://", "https://", "onclick=", "onload=", "fetch(", "xmlhttprequest", "localstorage", "sessionstorage")

def plain(s):
    return " ".join(re.sub(r"<[^>]+>"," ",s).split())

class OfferSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ru=(SITE/"offers-25.html").read_text(encoding="utf-8")
        cls.en=(SITE/"en"/"offers-25.html").read_text(encoding="utf-8")
        cls.data=json.loads(DATA.read_text(encoding="utf-8"))["offers"]

    def test_ru_page_exists_and_lang(self):
        self.assertIn('<html lang="ru">',self.ru)
        self.assertIn("25 офферов",plain(self.ru))

    def test_en_page_exists_and_lang(self):
        self.assertIn('<html lang="en">',self.en)
        self.assertIn("25 offers",plain(self.en))

    def test_titles_and_meta(self):
        for text in (self.ru,self.en):
            self.assertIn("<title>",text)
            self.assertIn('name="description"',text)

    def test_every_offer_id_is_rendered_both_languages(self):
        for offer in self.data:
            for text in (self.ru,self.en):
                self.assertIn(offer["id"],text)

    def test_prices_match_json(self):
        for offer in self.data:
            price=f"{offer['fixed_price_usd']:,}".replace(","," ")
            self.assertIn(price,self.ru,offer["id"])
            self.assertTrue(str(offer["fixed_price_usd"]) in self.en.replace(",", " ") or price in self.en,
                            offer["id"])

    def test_hypothesis_and_not_client_disclaimers(self):
        for text in (self.ru,self.en):
            body=plain(text).lower()
            for marker in ("research_only","admission=false","hypothesis","not_client=true"):
                self.assertIn(marker,body)

    def test_email_is_the_only_contact(self):
        for text in (self.ru,self.en):
            self.assertIn("mailto:flumos@gmail.com",text)
            self.assertNotRegex(text,r"\+\d[\d\s().-]{7,}")
            emails = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))
            self.assertLessEqual(emails, {"flumos@gmail.com"})
            self.assertIn("mailto:flumos@gmail.com", text)

    def test_no_forms_scripts_or_external_resources(self):
        for text in (self.ru,self.en):
            low=text.lower()
            for token in FORBIDDEN:
                self.assertNotIn(token,low,token)

    def test_no_client_or_covert_influence_claims(self):
        for text in (plain(self.ru).lower(),plain(self.en).lower()):
            for token in ("our client", "trusted by", "мы работали", "секретная сеть", "covert influence"):
                self.assertNotIn(token,text)

    def test_sitemap_contains_both_pages(self):
        sitemap=(ROOT/"site"/"sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("/business/offers-25.html</loc>",sitemap)
        self.assertIn("/business/en/offers-25.html</loc>",sitemap)

    def test_navigation_links_exist(self):
        for page in ("index.html","cases.html","pricing.html"):
            self.assertIn('href="offers-25.html"',(SITE/page).read_text(encoding="utf-8"))
            self.assertIn('href="offers-25.html"',(SITE/"en"/page).read_text(encoding="utf-8"))

    def test_catalog_source_is_not_misrepresented_as_client(self):
        for text in (plain(self.ru).lower(),plain(self.en).lower()):
            self.assertTrue("list price" in text or "list-price" in text or "наши цены" in text)
            self.assertTrue("not market" in text or "не рыноч" in text)

if __name__ == "__main__":
    unittest.main()
