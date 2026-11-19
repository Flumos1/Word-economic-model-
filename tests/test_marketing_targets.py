#!/usr/bin/env python3
"""Tests for the marketing target-account scoring tool."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "marketing_targets.py"
spec = importlib.util.spec_from_file_location("marketing_targets", TOOL)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class MarketingTargetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = mod.load_accounts()
        cls.report = mod.build_report(cls.rows, "2026-11-19T00:00:00Z")

    def test_twenty_accounts_unique(self):
        self.assertEqual(len(self.rows), 20)
        self.assertEqual(len({row["id"] for row in self.rows}), 20)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(mod.WEIGHTS.values()), 1.0, places=9)

    def test_score_formula(self):
        row = next(item for item in self.rows if item["id"] == "TATNEFT")
        expected = sum(mod.WEIGHTS[key] * row[key] for key in mod.WEIGHTS)
        self.assertAlmostEqual(mod.score(row), expected, places=3)

    def test_scores_bounded_and_sorted(self):
        scores = [item["score"] for item in self.report["all"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for value in scores:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 5.0)

    def test_primary_candidates(self):
        top = [item["id"] for item in self.report["top"][:4]]
        self.assertIn("TATNEFT", top)
        self.assertIn("GAZPROMNEFT", top)
        self.assertIn("TANECO", top)

    def test_tiers_match_thresholds(self):
        for item in self.report["all"]:
            if item["score"] >= mod.TIER_A:
                self.assertEqual(item["tier"], "A")
            elif item["score"] >= mod.TIER_B:
                self.assertEqual(item["tier"], "B")
            else:
                self.assertEqual(item["tier"], "C")

    def test_report_is_deterministic(self):
        again = mod.build_report(self.rows, "2026-11-19T00:00:00Z")
        self.assertEqual(json.dumps(self.report, ensure_ascii=False, sort_keys=True),
                         json.dumps(again, ensure_ascii=False, sort_keys=True))

    def test_markdown_contains_all_accounts(self):
        text = mod.render_markdown(self.report)
        self.assertIn("Целевые аккаунты", text)
        for item in self.rows:
            self.assertIn(item["name"], text)

    def test_validation_rejects_bad_score(self):
        rows = json.loads(json.dumps(self.rows))
        rows[0]["fit"] = 7
        with self.assertRaises(ValueError):
            mod.load_accounts_from_rows(rows) if hasattr(mod, "load_accounts_from_rows") else None
            # load_accounts only takes a path; exercise the same checks through a temp file.
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "bad.json"
                path.write_text(json.dumps(rows), encoding="utf-8")
                mod.load_accounts(path)

    def test_validation_rejects_duplicate(self):
        rows = json.loads(json.dumps(self.rows))
        rows[1]["id"] = rows[0]["id"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dup.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            with self.assertRaises(ValueError):
                mod.load_accounts(path)

    def test_cli_writes_markdown(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "targets.md"
            done = subprocess.run([sys.executable, str(TOOL), "--out", str(out),
                                   "--generated-at", "2026-11-19T00:00:00Z"],
                                  capture_output=True, text=True, timeout=60, cwd=str(ROOT))
            self.assertEqual(done.returncode, 0, done.stderr)
            text = out.read_text(encoding="utf-8")
            self.assertIn("Газпром нефть", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
