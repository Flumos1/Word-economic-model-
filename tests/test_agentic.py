"""Tests for the agentic ABM with SFC consistency (src/amfe/agentic/simulator.py).

Covered: parameter validation; double-entry ledger (unbalanced postings fail closed); financial
conservation and the closed-economy SFC identity ``S = I + CA``; the bank identity; determinism; price
convergence and the fail-closed iteration budget; credit-limit fail-closed; income-shock and
AI-procurement-share effects; the demo API and its text report.
"""
from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.agentic.simulator import (  # noqa: E402
    AgenticSimulator,
    ConvergenceError,
    Ledger,
    LedgerError,
    ParameterError,
    Parameters,
    SFCError,
    format_demo,
    run_demo,
)


class LedgerTests(unittest.TestCase):
    def test_balanced_transaction_is_recorded(self):
        ledger = Ledger()
        record = ledger.post("test", {"a": Decimal("10"), "b": Decimal("-10")})
        self.assertEqual(ledger.balance("a"), Decimal("10"))
        self.assertEqual(ledger.balance("b"), Decimal("-10"))
        self.assertEqual(ledger.total(), 0)
        self.assertEqual(len(ledger.transactions), 1)
        self.assertEqual(record["label"], "test")

    def test_unbalanced_transaction_fails_closed(self):
        ledger = Ledger()
        with self.assertRaises(LedgerError):
            ledger.post("bad", {"a": Decimal("10"), "b": Decimal("-9")})
        self.assertEqual(ledger.total(), 0)
        self.assertEqual(ledger.transactions, [])

    def test_digest_is_deterministic(self):
        first, second = Ledger(), Ledger()
        for ledger in (first, second):
            ledger.post("one", {"a": Decimal("1"), "b": Decimal("-1")})
            ledger.post("two", {"a": Decimal("-1"), "b": Decimal("1")})
        self.assertEqual(first.digest(), second.digest())
        second.post("three", {"a": Decimal("1"), "b": Decimal("-1")})
        self.assertNotEqual(first.digest(), second.digest())


class ParameterTests(unittest.TestCase):
    def test_invalid_parameters_fail_closed(self):
        for overrides in ({"n_households": 0}, {"n_firms": 0}, {"periods": 0},
                          {"ai_share_end": 1.5}, {"ai_switch_end": 1, "ai_switch_start": 5},
                          {"damping": 0.0}, {"max_iterations": 0},
                          {"shock_period": 20}, {"max_loan_per_firm": -1}):
            with self.assertRaises(ParameterError, msg=str(overrides)):
                Parameters(**overrides).validate()

    def test_default_parameters_are_valid(self):
        Parameters().validate()


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = AgenticSimulator(Parameters()).run()

    def test_periods_and_transactions(self):
        self.assertEqual(len(self.result.periods), Parameters().periods)
        self.assertGreater(self.result.transactions, 100)
        self.assertEqual(self.result.transactions, self.result.checks["transactions"])

    def test_closed_economy_sfc_identity_every_period(self):
        total_saving = Decimal(0)
        total_investment = Decimal(0)
        for period in self.result.periods:
            self.assertEqual(period.financial_total, 0)
            self.assertEqual(period.saving, period.investment)
            self.assertEqual(period.checks["current_account"], "0")
            total_saving += period.saving
            total_investment += period.investment
        self.assertEqual(total_saving, total_investment)
        self.assertTrue(self.result.checks["sfc_saving_equals_investment"])
        self.assertTrue(self.result.checks["closed_economy_no_external_accounts"])

    def test_all_checks_pass_and_prices_are_positive(self):
        for key in ("double_entry", "financial_conservation",
                    "sfc_saving_equals_investment",
                    "closed_economy_no_external_accounts", "convergence"):
            self.assertTrue(self.result.checks[key], key)
        for period in self.result.periods:
            self.assertGreater(period.price_index, 0)
            self.assertGreaterEqual(period.price_iterations, 1)
            self.assertLessEqual(period.price_iterations, Parameters().max_iterations)

    def test_ledger_accounts_and_digest(self):
        digest = self.result.ledger_digest
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in digest))

    def test_determinism(self):
        again = AgenticSimulator(Parameters()).run()
        self.assertEqual(again.ledger_digest, self.result.ledger_digest)
        self.assertEqual([period.as_dict() for period in again.periods],
                         [period.as_dict() for period in self.result.periods])
        other_seed = AgenticSimulator(Parameters(seed=7)).run()
        self.assertNotEqual(other_seed.ledger_digest, self.result.ledger_digest)


class FailClosedTests(unittest.TestCase):
    def test_convergence_budget_fails_closed(self):
        with self.assertRaises(ConvergenceError):
            AgenticSimulator(Parameters(damping=1.0, input_coeff=0.9, markup=1.4,
                                        max_iterations=4)).run()

    def test_credit_limit_fails_closed(self):
        with self.assertRaises(SFCError):
            AgenticSimulator(Parameters(max_loan_per_firm=0.0)).run()


class DemoEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = run_demo()

    def test_demo_runs_are_all_consistent(self):
        self.assertEqual(set(self.demo), {"baseline", "income_shock", "income_shock_ai"})
        for name, result in self.demo.items():
            self.assertTrue(result.checks["sfc_saving_equals_investment"], name)
            self.assertTrue(result.checks["financial_conservation"], name)
            self.assertTrue(result.checks["convergence"], name)

    def test_income_shock_lowers_employment_and_consumption(self):
        baseline = self.demo["baseline"].periods[-1]
        shock = self.demo["income_shock"].periods[-1]
        self.assertLess(shock.employed, baseline.employed)
        self.assertLess(shock.consumption, baseline.consumption)
        self.assertGreater(shock.unemployment_rate, 0)

    def test_ai_share_ramp_lowers_the_price_index(self):
        shock = self.demo["income_shock"]
        ai_run = self.demo["income_shock_ai"]
        self.assertEqual(ai_run.periods[0].ai_share, Decimal("0"))
        self.assertEqual(ai_run.periods[-1].ai_share, Decimal("0.5"))
        self.assertLess(ai_run.periods[-1].price_index, shock.periods[-1].price_index)
        self.assertTrue(all(period.ai_share >= 0 for period in ai_run.periods))

    def test_demo_result_is_json_serializable(self):
        payload = self.demo["income_shock_ai"].as_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("income_shock_ai", text)
        self.assertIn("sfc_saving_equals_investment", payload["checks"])

    def test_demo_text_report(self):
        report = format_demo(self.demo)
        self.assertIn("AMFE agentic simulator", report)
        self.assertIn("not a forecast", report)
        self.assertIn("sfc_saving_equals_investment=True", report)
        self.assertEqual(report, format_demo(self.demo))


if __name__ == "__main__":
    unittest.main(verbosity=2)
