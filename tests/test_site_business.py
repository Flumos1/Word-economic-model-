"""Commercial mini-site ``site/business/`` (RU + EN): structure, honesty, no external resources.

The commercial layer is a hand-written static mini-site inside the research publication. It must stay
a *shop window* of the same research: no admitted model, no invented clients or numbers, no CDN, no
JavaScript, no forms, no trackers — only HTML, one local stylesheet and a ``mailto:`` link.

The tests read the committed files (the mini-site is not generated at deploy time): if a page, the
stylesheet or an internal link is missing, the suite fails before anything is published.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUSINESS = ROOT / "site" / "business"
RESEARCH = ROOT / "site"

RU_PAGES = ("index", "solutions", "cases", "pricing", "security", "contact")
RU_FILES = tuple(f"{name}.html" for name in RU_PAGES)
EN_FILES = tuple(f"en/{name}.html" for name in RU_PAGES)
ALL_FILES = RU_FILES + EN_FILES
CSS = "assets/style.css"

CONTACT = "flumos@gmail.com"
PRICES = ("7500", "10000", "20000", "30000", "25000", "80000")

FORBIDDEN_RESOURCE_TOKENS = (
    "http://", "https://", "<script", "javascript:", "onclick=", "onload=", "onerror=",
    "@import", "<iframe", "<img", "<picture", "<object", "<embed", "<video", "<audio",
    "cdn.", "cdnjs", "jsdelivr", "unpkg", "fonts.googleapis", "fonts.gstatic",
    "google-analytics", "googletagmanager", "gtag(", "googlesyndication",
    "mc.yandex", "metrika", "hotjar", "clarity.ms", "doubleclick", "facebook.net",
    "connect.facebook", "analytics.js", "segment.io", "mixpanel", "amplitude",
)

FORBIDDEN_DATA_TOKENS = (
    "<form", "<input", "<button", "<textarea", "<select", "action=", "method=\"post\"",
    "fetch(", "xmlhttprequest", "localstorage", "sessionstorage", "indexeddb",
)

# Real companies are never claimed as clients or references by this site.
COMPANY_NAMES = (
    "газпром", "татнефть", "танеко", "новатэк", "лукойл", "роснефть", "сибур",
    "сургутнефтегаз", "зарубежнефть", "иркутская нефтяная", "башнефть",
    "gazprom", "tatneft", "taneco", "novatek", "lukoil", "rosneft", "sibur",
    "surgutneftegas", "bashneft",
)


def read(relative: str) -> str:
    return (BUSINESS / relative).read_text(encoding="utf-8")


def text_of(html: str) -> str:
    """Visible text with tags removed and whitespace collapsed."""
    plain = re.sub(r"<[^>]+>", " ", html)
    return " ".join(plain.split())


def hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', html)


def prices_seen(html: str) -> str:
    """Price string normalised across locales: '7 500 – 10 000' and '7,500 – 10,000' both -> numbers."""
    plain = text_of(html).replace("\u00a0", " ").replace("\u202f", " ")
    return re.sub(r"[ \u2009,]", "", plain)


class BusinessSite(unittest.TestCase):
    def setUp(self):
        self.ru = {name: read(name) for name in RU_FILES}
        self.en = {name: read(name) for name in EN_FILES}
        self.css = read(CSS)

    # ------------------------------------------------------------------------------ files/structure
    def test_pages_stylesheet_and_two_languages(self):
        for name in ALL_FILES:
            path = BUSINESS / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 900, name)
        self.assertTrue((BUSINESS / CSS).is_file(), CSS)
        self.assertGreater((BUSINESS / CSS).stat().st_size, 1500, CSS)
        self.assertEqual(len(RU_FILES), 6)
        self.assertEqual(len(EN_FILES), 6)

    def test_html_language_viewport_and_local_css(self):
        for name, page in self.ru.items():
            self.assertIn('<html lang="ru">', page, name)
            self.assertIn('<meta charset="utf-8">', page, name)
            self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">',
                          page, name)
            self.assertIn('<meta name="description"', page, name)
            self.assertIn('<meta name="theme-color"', page, name)
            self.assertIn("<title>", page, name)
            self.assertIn('href="assets/style.css"', page, name)
            self.assertNotIn('href="../assets/style.css"', page, name)
            self.assertIn("<main", page, name)
            self.assertIn("<nav", page, name)
        for name, page in self.en.items():
            self.assertIn('<html lang="en">', page, name)
            self.assertNotIn('<html lang="ru">', page, name)
            self.assertIn('href="../assets/style.css"', page, name)
            self.assertIn('<meta name="viewport"', page, name)
            self.assertIn("<main", page, name)

    def test_css_is_mobile_first_and_self_contained(self):
        lowered = self.css.lower()
        self.assertIn("prefers-color-scheme", lowered)
        self.assertIn("color-scheme", lowered)
        self.assertIn("box-sizing", lowered)
        self.assertNotIn("http", lowered)
        self.assertNotIn("url(", lowered)
        self.assertNotIn("@import", lowered)
        self.assertNotIn("expression(", lowered)
        self.assertNotIn("@media (max-width", lowered)
        self.assertGreaterEqual(lowered.count("@media (min-width"), 2)
        self.assertIn("min-width: 40rem", lowered)

    # ---------------------------------------------------------------- no CDN / JS / forms / trackers
    def test_no_external_resources_or_javascript(self):
        for name, page in {**self.ru, **self.en}.items():
            lowered = page.lower()
            for token in FORBIDDEN_RESOURCE_TOKENS:
                self.assertNotIn(token, lowered, f"{name} contains {token}")
            self.assertIsNone(re.search(r"\son[a-z]+\s*=\s*\"", lowered), f"{name} has an event handler")
            self.assertIsNone(re.search(r"\son[a-z]+\s*=\s*'", lowered), f"{name} has an event handler")

    def test_no_forms_or_client_side_state(self):
        for name, page in {**self.ru, **self.en}.items():
            lowered = page.lower()
            for token in FORBIDDEN_DATA_TOKENS:
                self.assertNotIn(token, lowered, f"{name} contains {token}")

    def test_contact_is_mailto_only(self):
        emails = set()
        for name, page in {**self.ru, **self.en}.items():
            links = [href for href in hrefs(page) if href.startswith("mailto:")]
            self.assertTrue(links, f"{name} has no mailto link")
            for link in links:
                address = link[len("mailto:"):].split("?")[0]
                self.assertEqual(address, CONTACT, f"{name} uses {address}")
            for match in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", page):
                emails.add(match.rstrip("."))
        self.assertEqual(emails, {CONTACT}, f"unexpected e-mail addresses: {sorted(emails)}")

    # ------------------------------------------------------------------------- honesty: no fake trust
    def test_no_client_logos_testimonials_or_company_names(self):
        for name, page in {**self.ru, **self.en}.items():
            lowered = page.lower()
            for name_token in COMPANY_NAMES:
                self.assertNotIn(name_token, lowered, f"{name} mentions {name_token}")
            for token in ("trusted by", "our clients", "клиенты:", "наш клиент",
                          "as seen in", "рекомендуют нас", "они нам доверяют"):
                self.assertNotIn(token, lowered, f"{name} contains {token}")
            self.assertNotIn("alt=\"logo", lowered, name)
        ru_index = text_of(self.ru["index.html"])
        en_index = text_of(self.en["en/index.html"])
        self.assertIn("Клиентских логотипов", ru_index)
        self.assertIn("no client logos", en_index.lower())

    def test_no_admitted_model_claims(self):
        for name, page in {**self.ru, **self.en}.items():
            lowered = page.lower()
            for token in ("admission=true", "admission: true", "admitted: true",
                          "допуск получен", "допущена к использованию", "model is admitted",
                          "admitted for use, and the", "active status"):
                self.assertNotIn(token, lowered, f"{name} contains {token}")
            self.assertIn("admission=false", lowered, name)
            self.assertIn("research_only", lowered, name)
        for name, page in self.ru.items():
            text = text_of(page)
            self.assertIn("не прогноз", text, name)
            self.assertIn("не инвестиционная рекомендация", text, name)
            self.assertIn("не допущена", text, name)
        for name, page in self.en.items():
            text = text_of(page).lower()
            self.assertIn("not a forecast", text, name)
            self.assertIn("investment advice", text, name)
            self.assertIn("not admitted", text, name)

    def test_disclaimer_is_on_every_page(self):
        for name, page in {**self.ru, **self.en}.items():
            self.assertIn('class="foot"', page, name)
            self.assertIn(CONTACT, page, name)
        for name in RU_FILES:
            self.assertIn("RESEARCH_ONLY", self.ru[name], name)
        for name in EN_FILES:
            self.assertIn("RESEARCH_ONLY", self.en[name], name)

    # -------------------------------------------------------------------------------- prices in USD
    def test_prices_are_usd_and_complete(self):
        for page_name in ("pricing.html", "en/pricing.html"):
            page = read(page_name)
            haystack = prices_seen(page)
            for price in PRICES:
                self.assertIn(price, haystack, f"{page_name} misses {price}")
            self.assertIn("USD", text_of(page), page_name)
            for token in ("₽", "руб.", "RUB", "рублей"):
                self.assertNotIn(token, page, f"{page_name} quotes {token}")

    def test_pricing_terms_and_exclusions(self):
        ru = text_of(self.ru["pricing.html"])
        en = text_of(self.en["en/pricing.html"])
        for token in ("не входит", "без НДС", "Срок", "ориентир", "SLA", "сторонние данные"):
            self.assertIn(token, ru, token)
        for token in ("Not included", "excluding VAT", "Timeline", "indicative", "SLA",
                      "third-party data"):
            self.assertIn(token, en, token)
        self.assertIn("0 (бесплатно)", ru)
        self.assertIn("25 000 – 80 000", ru)

    # ---------------------------------------------------------------------------------- page content
    def test_index_has_three_ctas_and_a_trust_block(self):
        ru = self.ru["index.html"]
        text = text_of(ru)
        self.assertIn("Сценарии для решений: бюджет, закупки, газ/СПГ, API", text)
        self.assertIn("20-минутная диагностика", text)
        self.assertIn("Пилот: USD 7 500 – 10 000", text)
        self.assertIn("NDA", text)
        self.assertIn("7 500 – 10 000", text)
        self.assertIn('href="pricing.html"', ru)
        self.assertIn('href="security.html"', ru)
        self.assertIn('href="../methodology.html"', ru)
        self.assertIn("SHA-256", text)
        self.assertIn("5.0.1", text)
        self.assertIn("FROZEN", text)
        self.assertIn("RESEARCH_ONLY", text)
        self.assertIn("admission=false", text)
        en = text_of(self.en["en/index.html"])
        self.assertIn("Scenarios for decisions: budget, procurement, gas/LNG, API", en)
        self.assertIn("20-minute diagnostic", en)
        self.assertIn("USD 7,500 – 10,000", en)

    def test_solutions_cover_four_directions(self):
        for page, tokens in ((self.ru["solutions.html"],
                              ("Ормуз", "FLAKS", "СПГ", "API", "TCO", "Границы")),
                             (self.en["en/solutions.html"],
                              ("Hormuz", "FLAKS", "LNG", "API", "TCO", "Limits"))):
            text = text_of(page)
            for token in tokens:
                self.assertIn(token, text, token)
            for anchor in ('id="hormuz"', 'id="flaks"', 'id="gas"', 'id="api"'):
                self.assertIn(anchor, page, anchor)
        for number in ("1.", "2.", "3.", "4."):
            self.assertIn(number, text_of(self.ru["solutions.html"]))

    def test_cases_are_honest_placeholders(self):
        for page, status in ((self.ru["cases.html"], "готовится"),
                             (self.en["en/cases.html"], "in progress")):
            text = text_of(page)
            self.assertIn("marketing/cases/", text)
            self.assertIn("site/business/cases/", text)
            self.assertIn(status, text)
            self.assertIsNone(re.search(r"\d+\s?%", text), "cases page must not cite percentages")
            self.assertIsNone(re.search(r"[$€]\s?\d", text), "cases page must not cite money")
        self.assertIn("Ни одной выдуманной цифры", text_of(self.ru["cases.html"]))
        self.assertIn("invented number", text_of(self.en["en/cases.html"]))
        self.assertNotIn("cases/01_", self.ru["cases.html"])

    def test_security_page_covers_data_rules(self):
        ru = text_of(self.ru["security.html"])
        en = text_of(self.en["en/security.html"])
        for token in ("NDA", "ANON-", "on-prem", "удал", "127.0.0.1", "SHA-256",
                      "Что не нужно присылать", "Обратное соответствие"):
            self.assertIn(token, ru, token)
        for token in ("NDA", "ANON-", "on-prem", "delet", "127.0.0.1", "SHA-256",
                      "What not to send", "reverse mapping"):
            self.assertIn(token, en, token)

    # ------------------------------------------------------------------------------------- links
    def test_all_internal_links_resolve(self):
        site_root = (RESEARCH).resolve()
        checked = 0
        for name in ALL_FILES:
            page_path = BUSINESS / name
            page = read(name)
            for href in hrefs(page):
                if href.startswith(("mailto:", "#")):
                    continue
                self.assertFalse(href.startswith("/"), f"{name}: absolute link {href}")
                target = (page_path.parent / href.split("#", 1)[0]).resolve()
                self.assertTrue(str(target).startswith(str(site_root)),
                                f"{name}: link escapes site/: {href}")
                self.assertTrue(target.is_file(), f"{name}: broken link {href}")
                checked += 1
        self.assertGreaterEqual(checked, 60, "the mini-site should cross-link its pages")

    def test_every_page_is_reachable_from_the_navigation(self):
        for name in ALL_FILES:
            page = read(name)
            for page_name in RU_PAGES:
                self.assertIn(f'href="{page_name}.html"', page, f"{name} -> {page_name}.html")
            self.assertIn('href="index.html"', page, name)

    def test_language_switch_links_point_to_the_same_page(self):
        for name in RU_FILES:
            page = self.ru[name]
            self.assertIn(f'href="en/{name}"', page, name)
            self.assertIn('hreflang="en"', page, name)
        for name in EN_FILES:
            page = self.en[name]
            bare = name.split("/", 1)[1]
            self.assertIn(f'href="../{bare}"', page, name)
            self.assertIn('hreflang="ru"', page, name)

    def test_research_site_links_to_the_commercial_layer(self):
        self.assertIn('href="business/index.html"', (RESEARCH / "index.html").read_text(encoding="utf-8"))
        self.assertIn('href="../business/en/index.html"',
                      (RESEARCH / "en" / "index.html").read_text(encoding="utf-8"))
        template = (ROOT / "tools" / "site_build.py").read_text(encoding="utf-8")
        self.assertIn('href="business/index.html"', template)
        self.assertIn('href="../business/en/index.html"', template)
        sitemap = (RESEARCH / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("/business/</loc>", sitemap)
        self.assertIn("/business/pricing.html</loc>", sitemap)
        self.assertIn("/business/en/</loc>", sitemap)

    def test_no_raw_data_paths_or_dumps(self):
        forbidden = ("Z_sparse", "stored_Z", "cells_Z", "dataset.json", "C:\\\\", "/home/",
                     "by_country", "va_by_country")
        for name, page in {**self.ru, **self.en}.items():
            for token in forbidden:
                self.assertNotIn(token, page, f"{name} contains {token}")
            self.assertIsNone(re.search(r"(?:-?\d+(?:\.\d+)?,\s*){12,}", page), name)

class Cases100Page(unittest.TestCase):
    """The published 100-case catalog page must stay honest and self-contained."""

    def test_pages_exist_and_counts(self):
        ru = read("cases-100.html")
        en = read("en/cases-100.html")
        for page, lang in ((ru, "ru"), (en, "en")):
            plain = text_of(page)
            self.assertIn("100", plain)
            self.assertIn("50", plain)
            self.assertIn("not client", plain)
            self.assertIn("HYPOTHESIS", plain)
            self.assertIn("NOT_FETCHED", plain)
            self.assertIn("RESEARCH_ONLY", plain)
            self.assertIn("admission=false", plain)
            self.assertIn(CONTACT, page)
            self.assertNotIn("50 из 100", plain)
            self.assertNotIn("50 of the 100", plain)

    def test_pdf_links_and_assets(self):
        for page in (read("cases-100.html"), read("en/cases-100.html")):
            self.assertIn("assets/cases/catalog.pdf", page)
            self.assertIn("assets/cases/catalog_en.pdf", page)
        for name in ("catalog.pdf", "catalog_en.pdf"):
            path = BUSINESS / "assets" / "cases" / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 50_000, name)

    def test_no_forms_scripts_or_external_resources(self):
        for page in (read("cases-100.html"), read("en/cases-100.html")):
            for token in FORBIDDEN_RESOURCE_TOKENS + FORBIDDEN_DATA_TOKENS:
                self.assertNotIn(token, page.lower(), token)

    def test_sitemap_includes_catalog_pages(self):
        sitemap = (RESEARCH / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("/business/cases-100.html</loc>", sitemap)
        self.assertIn("/business/en/cases-100.html</loc>", sitemap)

    def test_named_companies_are_hypotheses_not_clients(self):
        for page in (read("cases-100.html"), read("en/cases-100.html")):
            plain = text_of(page).lower()
            self.assertIn("гипотез", plain) if "гипотез" in plain else self.assertIn("hypothes", plain)
            for claim in ("наш клиент", "our client", "we worked with"):
                self.assertNotIn(claim, plain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
