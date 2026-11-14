"""Static Russian site (tools/site_build.py): pages, no JS/external URLs, consistency, determinism.

The site is a derived publication of registered results: every number must be traceable to the product
feed, the world reports, the evaluation reports or the shadow track; it must contain no raw third-party
values, no JavaScript and no external resources, and it must fail closed on a missing input.
"""
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import site_build  # noqa: E402

FIXED = "2026-11-14T12:00:00Z"
ANOTHER = "2026-11-14T18:00:00Z"

FEED = ROOT / "build" / "product" / "forecast_latest.json"
WORLD_INDEX = ROOT / "build" / "world" / "report_index.json"
SHADOW = ROOT / "build" / "shadow" / "shadow_status.json"
REPORT_V5 = ROOT / "protocols" / "first_real_evaluation_report_v5.json"
REPORT_V4 = ROOT / "protocols" / "regime_v4_candidate_evaluation_report.json"

PAGES = ("index.html", "model.html", "forecast.html", "scenarios.html", "data.html",
         "methodology.html", "docs.html")
EN_FILES = tuple(f"en/{name}" for name in PAGES)
REQUIRED_FILES = PAGES + EN_FILES + ("assets/style.css",)
ALL_FILES = REQUIRED_FILES
SOURCE_FILES = (
    "build/product/forecast_latest.json",
    "build/world/report_index.json",
    "build/world/reports/energy_minus5.json",
    "build/world/reports/import_plus10.json",
    "build/world/reports/import_plus20.json",
    "build/world/reports/fx_minus10_DE.json",
    "build/world/reports/fx_minus20_DE.json",
    "build/world/reports/fx_minus10_FR.json",
    "build/world/reports/fx_minus20_FR.json",
    "build/shadow/shadow_status.json",
    "protocols/first_real_evaluation_report_v5.json",
    "protocols/regime_v4_candidate_evaluation_report.json",
    "data/mrio/figaro/figaro_2019/manifest.json",
    "data/mrio/wiod_2016/wiod_2000/manifest.json",
    "data/mrio/wiod_2016/wiod_2007/manifest.json",
    "data/mrio/wiod_2016/wiod_2014/manifest.json",
    "data/mrio/reports/blocked.json",
)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def text_of(html: str) -> str:
    """Visible text with tags removed and whitespace collapsed (for stable token checks)."""
    plain = re.sub(r"<[^>]+>", " ", html)
    return " ".join(plain.split())


def minimal_root(base: Path, with_feed: bool = True) -> Path:
    root = Path(base) / "repo"
    for relative in SOURCE_FILES:
        source = ROOT / relative
        if not source.is_file():
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for relative in site_build.DOC_FILES:
        source = ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if not with_feed:
        (root / "build" / "product" / "forecast_latest.json").unlink(missing_ok=True)
    return root


class SiteBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.out = cls.base / "site"
        cls.code = site_build.main(["--out-dir", str(cls.out), "--generated-at", FIXED])
        cls.texts = {name: (cls.out / name).read_text(encoding="utf-8")
                     for name in REQUIRED_FILES}
        cls.feed = read_json(FEED)
        cls.world = read_json(WORLD_INDEX)
        cls.shadow = read_json(SHADOW)
        cls.report_v5 = read_json(REPORT_V5)
        cls.report_v4 = read_json(REPORT_V4)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------------------- structure
    def test_exit_code_and_all_pages(self):
        self.assertEqual(self.code, 0)
        for name in REQUIRED_FILES:
            path = self.out / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 1000, name)

    def test_html_is_russian_and_structural(self):
        for name in PAGES:
            page = self.texts[name]
            self.assertIn('<html lang="ru">', page, name)
            self.assertIn('<meta charset="utf-8">', page, name)
            self.assertIn('name="viewport"', page, name)
            self.assertIn("<nav", page, name)
            self.assertIn("<main", page, name)
            self.assertIn('<footer class="site-footer">', page, name)
            for target in PAGES:
                self.assertIn(f'href="{target}"', page, f"{name} -> {target}")
        self.assertIn("AMFE: исследовательская модель мировой экономики", self.texts["index.html"])

    def test_statuses_and_disclaimer(self):
        index = text_of(self.texts["index.html"])
        forecast = text_of(self.texts["forecast.html"])
        methodology = text_of(self.texts["methodology.html"])
        for text in (index, forecast, methodology):
            self.assertIn("CANDIDATE", text)
            self.assertIn("не инвестиционная рекомендация", text)
            self.assertIn("не допуск", text.lower())
        self.assertIn("SHADOW_TEST", index)
        self.assertIn("SHADOW_TEST", forecast)
        self.assertIn("ENSEMBLE_REGIME_V4", forecast)
        for name in PAGES:
            self.assertIn("не инвестиционная рекомендация", self.texts[name], name)

    # ------------------------------------------------------------------------ no JS / no URLs
    def test_no_external_resources_and_no_javascript(self):
        forbidden = ("http://", "https://", "<script", "javascript:", "onclick=", "onload=",
                     "cdn.", "@import url(", "<iframe", "react", "vue.js")
        for name, content in self.texts.items():
            lowered = content.lower()
            for token in forbidden:
                self.assertNotIn(token, lowered, f"{name} contains {token}")
            if name.endswith(".html"):
                self.assertIn("assets/style.css", content, name)
            else:
                self.assertNotIn("url(", lowered)

    def test_stylesheet_is_local_adaptive_and_clean(self):
        css = self.texts["assets/style.css"].lower()
        self.assertIn("@media", css)
        self.assertIn("prefers-color-scheme", css)
        self.assertIn("color-scheme", css)
        self.assertNotIn("http", css)
        self.assertNotIn("url(", css)
        self.assertNotIn("expression(", css)

    # --------------------------------------------------------------------------- consistency
    def test_forecast_numbers_match_the_feed(self):
        forecast = text_of(self.texts["forecast.html"])
        quantiles = self.feed["quantiles_5_to_95_step_5"]
        self.assertIn(f"{self.feed['point']:.4f}", forecast)
        self.assertIn(f"{quantiles[0]:.4f}", forecast)
        self.assertIn(f"{quantiles[-1]:.4f}", forecast)
        self.assertIn(f"{quantiles[4]:.4f}", forecast)
        self.assertIn(f"{quantiles[14]:.4f}", forecast)
        for arm in ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"):
            self.assertIn(f"{self.feed['controls'][arm]['point']:.4f}", forecast)
        self.assertIn("1.0775", forecast)
        self.assertIn("0.9303", forecast)
        c6 = self.shadow["c6"]
        self.assertIn(f'{c6["n_resolved"]} / {c6["min_forecasts"]}', forecast)
        self.assertIn(f'{c6["distinct_target_periods"]} / {c6["min_distinct_target_periods"]}',
                      forecast)
        self.assertIn("C6", forecast)
        self.assertIn(self.feed["protocol"]["version"], forecast)

    def test_world_numbers_match_the_reports(self):
        index = text_of(self.texts["index.html"])
        data = text_of(self.texts["data.html"])
        dataset = self.world["dataset"]
        self.assertIn(f"{dataset['countries']} × {dataset['industries']}", index)
        self.assertIn(f"{dataset['countries']} × {dataset['industries']}", data)
        self.assertIn("87.3%", index)
        self.assertIn("87.3%", data)
        self.assertIn("44 × 56", data)
        self.assertIn("84.7%", data)
        self.assertIn("89.0%", data)
        self.assertIn("7", index)

    def test_all_scenarios_are_present(self):
        scenarios = text_of(self.texts["scenarios.html"])
        for scenario in self.world["scenarios"]:
            self.assertIn(str(scenario["id"]), scenarios)
        for value in ("-253 452", "-2 859 371", "-5 560 552", "CPA_D35", "RESEARCH_ONLY"):
            self.assertIn(value, scenarios)
        self.assertIn("сравнительная статика", scenarios.lower())

    def test_data_page_lists_sources_and_licences(self):
        data = text_of(self.texts["data.html"])
        for token in ("FIGARO", "WIOD", "OECD", "Eora", "Comtrade", "CC BY 4.0",
                      "AVAILABLE_AUTO_2016", "BLOCKED_LICENCE"):
            self.assertIn(token, data)
        self.assertIn("не публикуются", data.lower())

    def test_docs_page_links_are_local_and_resolve(self):
        page = self.texts["docs.html"]
        targets = re.findall(r'href="(\.\./docs/[^"]+)"', page)
        self.assertGreaterEqual(len(targets), 10)
        for target in targets:
            local = (ROOT / target[3:]).resolve()
            self.assertTrue(local.is_file(), target)
        for number in ("120", "125", "129", "131"):
            self.assertRegex(page, rf"docs/{number}_")

    # ------------------------------------------------------------------------------ english version
    def test_english_pages_exist_and_are_marked_en(self):
        for name in EN_FILES:
            content = self.texts[name]
            self.assertIn('<html lang="en">', content, name)
            self.assertNotIn('<html lang="ru">', content, name)
            self.assertIn("../assets/style.css", content, name)

    def test_english_disclaimer_and_statuses(self):
        for name in EN_FILES:
            text = text_of(self.texts[name])
            self.assertIn("Research only", text, name)
            self.assertIn("not admission", text, name)
            self.assertIn("not investment advice", text, name)
        index = text_of(self.texts["en/index.html"])
        forecast = text_of(self.texts["en/forecast.html"])
        self.assertIn("CANDIDATE", index)
        self.assertIn("SHADOW_TEST", index)
        self.assertIn("ENSEMBLE_REGIME_V4", forecast)
        self.assertIn("C4", forecast)
        self.assertIn("FAIL", forecast)
        self.assertIn("C6", forecast)

    def test_language_switcher_on_all_pages(self):
        for name in PAGES:
            ru_page = self.texts[name]
            self.assertIn('class="lang-switch"', ru_page, name)
            self.assertIn(f'href="en/{name}"', ru_page, name)
            self.assertIn('hreflang="en"', ru_page, name)
            self.assertIn('<html lang="ru">', ru_page, name)
        for name in EN_FILES:
            en_page = self.texts[name]
            page_name = name.split("/", 1)[1]
            self.assertIn('class="lang-switch"', en_page, name)
            self.assertIn(f'href="../{page_name}"', en_page, name)
            self.assertIn('hreflang="ru"', en_page, name)

    def test_english_numbers_match_the_sources(self):
        forecast = text_of(self.texts["en/forecast.html"])
        quantiles = self.feed["quantiles_5_to_95_step_5"]
        self.assertIn(f"{self.feed['point']:.4f}", forecast)
        self.assertIn(f"{quantiles[0]:.4f}", forecast)
        self.assertIn(f"{quantiles[-1]:.4f}", forecast)
        for arm in ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"):
            self.assertIn(f"{self.feed['controls'][arm]['point']:.4f}", forecast)
        self.assertIn("1.0775", forecast)
        self.assertIn("0.9303", forecast)
        c6 = self.shadow["c6"]
        self.assertIn(f'{c6["n_resolved"]} / {c6["min_forecasts"]}', forecast)
        self.assertIn(f'{c6["distinct_target_periods"]} / {c6["min_distinct_target_periods"]}',
                      forecast)
        index = text_of(self.texts["en/index.html"])
        data = text_of(self.texts["en/data.html"])
        dataset = self.world["dataset"]
        self.assertIn(f"{dataset['countries']} × {dataset['industries']}", index)
        self.assertIn("87.3%", index)
        self.assertIn("44 × 56", data)
        self.assertIn("84.7%", data)
        self.assertIn("89.0%", data)

    def test_english_scenarios_and_docs(self):
        scenarios = text_of(self.texts["en/scenarios.html"])
        for scenario in self.world["scenarios"]:
            self.assertIn(str(scenario["id"]), scenarios)
        for value in ("-253 452", "-2 859 371", "RESEARCH_ONLY"):
            self.assertIn(value, scenarios)
        page = self.texts["en/docs.html"]
        targets = re.findall(r'href="(\.\./\.\./docs/[^"]+)"', page)
        self.assertGreaterEqual(len(targets), 10)
        for target in targets:
            local = (ROOT / target[6:]).resolve()
            self.assertTrue(local.is_file(), target)

    # ------------------------------------------------------------------------- licence hygiene
    def test_no_raw_third_party_values(self):
        forbidden = ("Z_sparse", "stored_Z", "cells_Z", "by_country", "by_industry",
                     "va_by_country", "x_by_country", "exports_by_country", "dataset.json",
                     "total_value_added", "per_country_gdp_usd")
        for name, content in self.texts.items():
            for token in forbidden:
                self.assertNotIn(token, content, f"{name} contains {token}")
        # no dense numeric dumps: no run of 12+ comma-separated numbers
        for name, content in self.texts.items():
            self.assertIsNone(re.search(r"(?:-?\d+(?:\.\d+)?,\s*){12,}", content), name)

    # ------------------------------------------------------------------- invariants / CLI
    def test_determinism_except_generated_at(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertEqual(site_build.main(["--out-dir", first, "--generated-at", FIXED]), 0)
            self.assertEqual(site_build.main(["--out-dir", first, "--generated-at", FIXED]), 0)
            self.assertEqual(site_build.main(["--out-dir", second, "--generated-at", ANOTHER]), 0)
            for name in REQUIRED_FILES:
                left = (Path(first) / name).read_text(encoding="utf-8")
                right = (Path(second) / name).read_text(encoding="utf-8")
                self.assertEqual(left.replace(FIXED, "<generated_at>"),
                                 right.replace(ANOTHER, "<generated_at>"), name)
                self.assertEqual(left, (Path(first) / name).read_text(encoding="utf-8"), name)

    def test_fails_closed_on_missing_inputs(self):
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(io.StringIO()):
            code = site_build.main(["--root", empty, "--out-dir", str(Path(empty) / "site"),
                                    "--generated-at", FIXED])
        self.assertEqual(code, 2)
        self.assertFalse((Path(empty) / "site" / "index.html").exists())

        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base), with_feed=False)
            with contextlib.redirect_stderr(io.StringIO()):
                code = site_build.main(["--root", str(root), "--out-dir", str(root / "site"),
                                        "--generated-at", FIXED])
            self.assertEqual(code, 2)
            self.assertFalse((root / "site" / "index.html").exists())

    def test_inputs_are_not_modified(self):
        def digest(path: Path) -> str:
            import hashlib
            return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()

        before = {relative: digest(ROOT / relative) for relative in SOURCE_FILES}
        site_build.build_site(ROOT, out_dir=self.base / "again", generated_at=FIXED)
        for relative, value in before.items():
            self.assertEqual(digest(ROOT / relative), value, relative)

    def test_cli_runs_as_a_script(self):
        with tempfile.TemporaryDirectory() as out:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "site_build.py"),
                 "--out-dir", out, "--generated-at", FIXED],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["ok"])
            self.assertEqual(len(summary["files"]), len(REQUIRED_FILES))
            for name in REQUIRED_FILES:
                self.assertTrue((Path(out) / name).is_file(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
