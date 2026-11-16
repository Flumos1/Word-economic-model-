"""Tests for the G2 bridge: ABM ledger -> standard SFC sector accounts (accounts_bridge.py).

Covered: mapping completeness and vocabulary; preservation of sums; double-entry replay; financial
mirroring; the closed-economy SFC identity ``S = I + CA``; net-worth identities; export/JSON
serialisation; determinism; fail-closed on unknown accounts, a mismatched ledger and a broken
transaction; the three demo scenarios and the CLI tool.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.agentic.accounts_bridge import (  # noqa: E402
    SECTORS,
    AccountAggregates,
    AccountsBridge,
    AccountsBridgeError,
    build_account_aggregates,
    export_sector_accounts,
    render_report,
    validate_against_sfc,
)
from amfe.agentic.simulator import AgenticSimulator, Parameters  # noqa: E402

D = Decimal
PARAMS = Parameters(periods=16)
VALID_DIMENSIONS = {"current", "capital", "real", "financial"}
VALID_CODES = {"output", "wages", "direct_tax", "transfer", "interest",
               "household_consumption", "government_consumption",
               "intermediate_consumption", "fixed_investment", "investment_goods",
               "deposit", "loan", "fixed_capital"}
VALID_CATEGORIES = {"income", "expenditure", "financial", "real", "memo"}


class BridgeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.simulator = AgenticSimulator(PARAMS)
        cls.result = cls.simulator.run()
        cls.aggregates = build_account_aggregates(cls.result, cls.simulator.ledger)

    # ------------------------------------------------------------------------ mapping/sums
    def test_every_ledger_account_is_mapped(self):
        self.assertEqual(len(self.aggregates.mapping), len(self.simulator.ledger.accounts))
        self.assertEqual({entry["account"] for entry in self.aggregates.mapping},
                         set(self.simulator.ledger.accounts))
        for entry in self.aggregates.mapping:
            self.assertIn(entry["dimension"], VALID_DIMENSIONS, entry)
            self.assertIn(entry["code"], VALID_CODES, entry)
            self.assertIn(entry["category"], VALID_CATEGORIES, entry)
            if entry["sector"] is not None:
                self.assertIn(entry["sector"], SECTORS, entry)

    def test_sector_accounts_preserve_sums(self):
        self.assertEqual(set(self.aggregates.sectors), set(SECTORS))
        total_current = D(0)
        total_financial = D(0)
        for name in SECTORS:
            item = self.aggregates.sectors[name]
            self.assertEqual(item.income, sum(item.current.get(key, D(0))
                                              for key in item.current if key.endswith(":income")))
            self.assertEqual(item.expenditure,
                             sum(item.current.get(key, D(0))
                                 for key in item.current if key.endswith(":expenditure")))
            total_current += item.income + item.expenditure
            total_financial += item.financial_assets + item.financial_liabilities
        self.assertEqual(total_current, self.aggregates.totals["saving"])
        self.assertEqual(total_financial, self.aggregates.totals["net_financial_worth"])

    def test_double_entry_replay(self):
        self.assertTrue(self.aggregates.checks["double_entry"])
        for record in self.simulator.ledger.transactions:
            self.assertEqual(sum((D(value) for value in record["postings"].values()), D(0)), 0)
        self.assertTrue(self.aggregates.checks["ledger_total"])
        self.assertEqual(self.simulator.ledger.total(), 0)

    # ------------------------------------------------------------------------------ SFC
    def test_sfc_identity_saving_equals_investment(self):
        totals = self.aggregates.totals
        self.assertEqual(totals["current_account"], 0)
        self.assertEqual(totals["saving"], totals["investment"] + totals["current_account"])
        self.assertGreater(totals["investment"], 0)
        self.assertTrue(self.aggregates.checks["sfc_identity"])
        self.assertTrue(self.aggregates.checks["closed_economy"])

    def test_financial_mirroring(self):
        net = self.aggregates.totals["net_financial_worth"]
        self.assertEqual(net, 0)
        self.assertTrue(self.aggregates.checks["financial_mirroring"])
        bank = self.aggregates.sectors["bank"]
        self.assertEqual(bank.net_financial_worth, bank.financial_assets
                         + bank.financial_liabilities)

    def test_net_worth_identities(self):
        totals = self.aggregates.totals
        for name in SECTORS:
            item = self.aggregates.sectors[name]
            self.assertEqual(item.saving, item.income + item.expenditure, name)
            self.assertEqual(item.net_worth,
                             item.net_financial_worth + item.real_assets, name)
        self.assertEqual(totals["net_worth"], totals["real_assets"])
        self.assertTrue(self.aggregates.checks["net_worth"])

    def test_result_consistency_with_simulation(self):
        period_saving = sum((period.saving for period in self.result.periods), D(0))
        period_investment = sum((period.investment for period in self.result.periods), D(0))
        self.assertEqual(period_saving, self.aggregates.totals["saving"])
        self.assertEqual(period_investment, self.aggregates.totals["investment"])
        self.assertEqual(self.aggregates.ledger_digest, self.simulator.ledger.digest())
        self.assertEqual(self.aggregates.transactions, self.simulator.ledger.transaction_count())
        self.assertTrue(self.aggregates.checks["result_consistency"])

    # ------------------------------------------------------------------- export / determinism
    def test_export_and_json_are_serialisable(self):
        exported = export_sector_accounts(self.result, self.simulator.ledger)
        self.assertEqual(set(exported), set(SECTORS))
        self.assertEqual(exported["firm"]["saving"], str(self.aggregates.sectors["firm"].saving))
        payload = json.loads(self.aggregates.to_json())
        self.assertEqual(payload["schema"], "AMFE-AGENTIC-ACCOUNTS/1.0.0")
        self.assertEqual(set(payload["sectors"]), set(SECTORS))
        self.assertEqual(payload["checks"], self.aggregates.checks)

    def test_validate_against_sfc_returns_checks(self):
        checks = validate_against_sfc(self.result, self.simulator.ledger)
        self.assertEqual(checks, self.aggregates.checks)
        self.assertTrue(all(checks.values()))

    def test_determinism(self):
        other = AgenticSimulator(PARAMS)
        other_result = other.run()
        other_aggregates = build_account_aggregates(other_result, other.ledger)
        self.assertEqual(other_aggregates.as_dict(), self.aggregates.as_dict())
        self.assertEqual(render_report(other_aggregates), render_report(self.aggregates))

    def test_report_mentions_disclaimer_and_totals(self):
        report = render_report(self.aggregates)
        self.assertIn("ABM -> SFC accounts", report)
        self.assertIn("not a forecast", report)
        self.assertIn("CA=0", report)

    # ---------------------------------------------------------------------- fail-closed
    def test_unknown_account_fails_closed(self):
        simulator = AgenticSimulator(PARAMS)
        result = simulator.run()
        simulator.ledger.accounts["mystery.account"] = D(1)
        with self.assertRaises(AccountsBridgeError):
            build_account_aggregates(result, simulator.ledger)

    def test_mismatched_ledger_fails_closed(self):
        other = AgenticSimulator(Parameters(periods=16, seed=7))
        other.run()
        with self.assertRaises(AccountsBridgeError):
            build_account_aggregates(self.result, other.ledger)

    def test_broken_transaction_fails_closed(self):
        simulator = AgenticSimulator(PARAMS)
        result = simulator.run()
        simulator.ledger.transactions.append(
            {"label": "bad", "postings": {"hh.0.deposit": "1", "firm.0.deposit": "-0.5"}})
        with self.assertRaises(AccountsBridgeError):
            build_account_aggregates(result, simulator.ledger)

    def test_type_validation(self):
        with self.assertRaises(AccountsBridgeError):
            AccountsBridge(self.result, object())
        with self.assertRaises(AccountsBridgeError):
            AccountsBridge(object(), self.simulator.ledger)


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {}
        for name, overrides in (
                ("baseline", {"ai_share_start": 0.0, "ai_share_end": 0.0,
                              "shock_employment_drop": 0.0}),
                ("income_shock", {"ai_share_start": 0.0, "ai_share_end": 0.0}),
                ("income_shock_ai", {})):
            params = Parameters(**{**PARAMS.__dict__, **overrides,
                                   "demo_label": name})
            simulator = AgenticSimulator(params)
            result = simulator.run()
            cls.runs[name] = build_account_aggregates(result, simulator.ledger)

    def test_all_scenarios_satisfy_sfc(self):
        for name, aggregates in self.runs.items():
            self.assertTrue(all(aggregates.checks.values()), name)
            self.assertEqual(aggregates.totals["saving"], aggregates.totals["investment"], name)
            self.assertEqual(aggregates.totals["net_financial_worth"], 0, name)

    def test_shock_changes_income_and_investment(self):
        baseline = self.runs["baseline"].totals
        shock = self.runs["income_shock"].totals
        self.assertNotEqual(baseline["income"], shock["income"])
        self.assertLess(shock["investment"], baseline["investment"])
        self.assertEqual(self.runs["income_shock_ai"].totals["saving"],
                         self.runs["income_shock_ai"].totals["investment"])

    def test_aggregates_are_frozen_dataclass(self):
        self.assertIsInstance(self.runs["baseline"], AccountAggregates)
        self.assertTrue(self.runs["baseline"].schema.startswith("AMFE-AGENTIC-ACCOUNTS"))


class DemoToolTests(unittest.TestCase):
    def test_cli_text_and_json(self):
        environment = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
        text = subprocess.run([sys.executable,
                               str(ROOT / "tools" / "agentic_accounts_demo.py")],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", env=environment)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("S = I + CA (CA = 0): OK", text.stdout)
        self.assertIn("all aggregate checks: PASS", text.stdout)
        payload_run = subprocess.run([sys.executable,
                                      str(ROOT / "tools" / "agentic_accounts_demo.py"), "--json"],
                                     cwd=str(ROOT), capture_output=True, text=True,
                                     encoding="utf-8", env=environment)
        self.assertEqual(payload_run.returncode, 0, payload_run.stderr)
        payload = json.loads(payload_run.stdout)
        self.assertTrue(payload["all_checks_pass"])
        self.assertEqual(set(payload["runs"]), {"baseline", "income_shock", "income_shock_ai"})
        for name in payload["runs"]:
            self.assertTrue(payload["runs"][name]["checks"]["sfc_identity"], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
