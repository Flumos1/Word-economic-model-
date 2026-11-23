#!/usr/bin/env python3
"""Tests for the 25 fixed-price B2B/B2G offer catalogue."""
from __future__ import annotations
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "marketing" / "commercial_offers_25" / "offers.json"
TOOL = ROOT / "tools" / "commercial_offers.py"
spec = importlib.util.spec_from_file_location("commercial_offers", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def load():
    return json.loads(DATA.read_text(encoding="utf-8"))


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


class OfferData(unittest.TestCase):
    def test_schema_and_count(self):
        data = load()
        self.assertEqual(data["schema"], "amfe.commercial_offers.v1")
        self.assertEqual(len(data["offers"]), 25)
        self.assertEqual(data["offers_count"], 25)
        self.assertEqual(data["currency"], "USD")
        self.assertIs(data["taxes_included"], False)

    def test_ids_are_consecutive(self):
        ids = [o["id"] for o in load()["offers"]]
        self.assertEqual(ids, [f"C25-{i:03d}" for i in range(1, 26)])

    def test_buyer_coverage(self):
        offers = load()["offers"]
        self.assertGreaterEqual(len({o["buyer_org_type"] for o in offers}), 8)
        self.assertGreaterEqual(len({o["segment"] for o in offers}), 5)

    def test_all_are_hypotheses_not_clients(self):
        for o in load()["offers"]:
            self.assertEqual(o["evidence_status"], "HYPOTHESIS")
            self.assertIs(o["not_client"], True)
            self.assertIs(o["sent"], False)
            self.assertEqual(o["status"], "DRAFT")

    def test_fixed_price_is_integer_and_term_explained(self):
        for o in load()["offers"]:
            self.assertIsInstance(o["fixed_price_usd"], int)
            self.assertGreater(o["fixed_price_usd"], 0)
            self.assertIn(str(o["fixed_price_usd"]), o["price_terms_ru"].replace(" ", ""))

    def test_duration_matches_official_rate_card(self):
        for o in load()["offers"]:
            price, weeks = o["fixed_price_usd"], o["duration_weeks"]
            self.assertTrue(any(lo <= price <= hi and wlo <= weeks <= whi
                                for lo, hi, wlo, whi in mod.PRICE_BANDS), o["id"])

    def test_buyer_roles_not_person_names(self):
        for o in load()["offers"]:
            self.assertGreaterEqual(len(o["buyer_role"]), 5)
            self.assertNotRegex(o["buyer_role"], r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
            self.assertNotRegex(o["buyer_role"], r"\+\d[\d\s().-]{7,}")

    def test_titles_questions_and_deliverables_are_bilingual(self):
        for o in load()["offers"]:
            for field in ("title_ru", "title_en", "question_ru", "question_en",
                          "deliverables_ru", "deliverables_en"):
                self.assertTrue(o.get(field), f"{o['id']} {field}")

    def test_scope_has_modules_and_exclusions(self):
        for o in load()["offers"]:
            self.assertTrue(o["modules"])
            self.assertTrue(o["assumptions_ru"])
            self.assertTrue(o["exclusions_ru"])

    def test_no_contacts_or_external_urls(self):
        text = json.dumps(load(), ensure_ascii=False)
        self.assertNotRegex(text, mod.EMAIL_RE)
        self.assertNotRegex(text, mod.PHONE_RE)
        self.assertNotRegex(text, mod.URL_RE)

    def test_no_positive_admission_or_roi_claims(self):
        text = json.dumps(load(), ensure_ascii=False).lower()
        for token in ("admitted", "гарантируем", "we guarantee", "guaranteed savings",
                      "guaranteed accuracy", "мы работали", "our client"):
            self.assertNotIn(token.lower(), text)

    def test_public_org_types_not_named_organizations(self):
        for offer in load()["offers"]:
            self.assertTrue(offer["buyer_org_type"])
            self.assertTrue(offer["jurisdiction_scope"])

    def test_price_terms_disclaim_market_benchmark(self):
        for offer in load()["offers"]:
            joined = offer["price_terms_en"].lower()
            self.assertIn("fixed list price", joined)
            self.assertIn("not a market", joined)

    def test_validate_current_pack(self):
        self.assertEqual(mod.validate(load()), [])


class CatalogTool(unittest.TestCase):
    def test_cli_check(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("25 fixed-price offers", result.stdout)

    def test_cli_json(self):
        result = run("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["offers"], 25)

    def test_catalog_files_exist(self):
        for name in ("catalog.md", "catalog_en.md"):
            path = DATA.parent / name
            self.assertTrue(path.is_file(), name)
            self.assertIn("C25-025", path.read_text(encoding="utf-8"))

    def test_ru_catalog_has_fixed_prices_and_disclaimers(self):
        text = (DATA.parent / "catalog.md").read_text(encoding="utf-8").lower()
        for token in ("фиксирован", "not_client=true", "hypothesis", "usd", "не является прогнозом"):
            self.assertIn(token.lower(), text)

    def test_en_catalog_has_fixed_prices_and_disclaimers(self):
        text = (DATA.parent / "catalog_en.md").read_text(encoding="utf-8").lower()
        for token in ("list prices", "not_client=true", "hypothesis", "usd", "not a forecast"):
            self.assertIn(token.lower(), text)

    def test_renderer_is_deterministic(self):
        data = load()
        self.assertEqual(mod.render(data, "ru"), mod.render(data, "ru"))
        self.assertEqual(mod.render(data, "en"), mod.render(data, "en"))

    def test_rejects_wrong_count(self):
        data = load()
        data["offers"].pop()
        self.assertTrue(any("exactly 25" in e for e in mod.validate(data)))

    def test_rejects_wrong_price_duration(self):
        data = load()
        data["offers"][0]["duration_weeks"] = 2
        self.assertTrue(any(data["offers"][0]["id"] in e for e in mod.validate(data)))

    def test_rejects_contacts(self):
        data = load()
        data["offers"][0]["buyer_role"] = "buyer@example.com"
        self.assertTrue(any("contact" in e for e in mod.validate(data)))

    def test_rejects_client_claim(self):
        data = load()
        data["offers"][0]["title_en"] = "Our client: trusted forecast"
        self.assertTrue(mod.validate(data))

    def test_rejects_sent_offer(self):
        data = load()
        data["offers"][0]["sent"] = True
        self.assertTrue(mod.validate(data))

    def test_rejects_unknown_module(self):
        data = load()
        data["offers"][0]["modules"] = ["secret_influence"]
        self.assertTrue(mod.validate(data))


if __name__ == "__main__":
    unittest.main()
