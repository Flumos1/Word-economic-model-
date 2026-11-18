"""Tests for the research-only monetary/fiscal policy block (docs/177 blocks 11/12).

The block has no data and no network: everything here runs on the declared assumptions of
``amfe.world.policy``.  The tests cover the identities (Taylor fixed point, central-bank balance
sheet, r-g debt/deficit, SFC), the signs of the three MVP scenarios, determinism, the absence of raw
data in the reports and the fail-closed CLI behaviour.
"""
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import policy_scenarios as cli  # noqa: E402
from amfe.world import policy as pol  # noqa: E402
from amfe.dynamics.debt import debt_path as reference_debt_path  # noqa: E402

PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v5.json"
PROTOCOL_HASH = ROOT / "protocols" / "evaluation_protocol_v5.sha256"
FORBIDDEN_KEYS = {
    "Z", "x", "y", "va", "raw", "raw_data", "observations", "micro", "rows", "series",
    "by_sector", "delta_A", "gross_output_vector", "final_demand_vector", "Z_sparse",
}
_RUN = {}


def fixture():
    if not _RUN:
        _RUN.update(pol.run_all())
    return _RUN


def by_id(result):
    return {report["scenario"]["scenario_id"]: report for report in result["reports"]}


def forbidden_keys(node, path=""):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS:
                found.append(f"{path}/{key}")
            found.extend(forbidden_keys(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(forbidden_keys(value, f"{path}[{index}]"))
    return found


class TaylorRuleTests(unittest.TestCase):
    def test_fixed_point_is_the_neutral_rate(self):
        rule = pol.TaylorRuleWithInertia()
        self.assertAlmostEqual(rule.neutral_rate, 2.5)
        self.assertAlmostEqual(rule.rule_rate(2.0, 0.0), 2.5)
        self.assertAlmostEqual(rule.step(rule.neutral_rate, 2.0, 0.0), 2.5)

    def test_inertia_lies_between_previous_and_rule_and_converges(self):
        rule = pol.TaylorRuleWithInertia(inertia=0.8)
        target = rule.rule_rate(4.0, 1.0)  # 0.5 + 4 + 3 + 0.5 = 8.0
        first = rule.step(2.5, 4.0, 1.0)
        self.assertGreater(first, 2.5)
        self.assertLess(first, target)
        rate = 2.5
        for _ in range(1000):
            rate = rule.step(rate, 4.0, 1.0)
        self.assertAlmostEqual(rate, target, places=9)

    def test_taylor_principle_and_bounds(self):
        self.assertTrue(pol.TaylorRuleWithInertia(phi_pi=1.5).taylor_principle)
        self.assertFalse(pol.TaylorRuleWithInertia(phi_pi=0.9).taylor_principle)
        rule = pol.TaylorRuleWithInertia()
        self.assertEqual(rule.step(2.5, -10.0, -20.0), rule.lower_bound)
        self.assertEqual(rule.step(2.5, 60.0, 30.0), rule.upper_bound)

    def test_invalid_parameters_raise(self):
        for kwargs in ({"phi_pi": 0.0}, {"phi_y": -0.1}, {"inertia": 1.0},
                       {"pi_target": 0.0}, {"lower_bound": 5.0, "upper_bound": 5.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(pol.PolicyError):
                pol.TaylorRuleWithInertia(**kwargs)


class CentralBankTests(unittest.TestCase):
    def test_balance_sheet_identity(self):
        cb = pol.CentralBankBalance(bonds=30.0, fx_reserves=5.0, loans=2.0,
                                    currency=10.0, reserves=27.0)
        check = cb.check()
        self.assertTrue(check["ok"])
        self.assertAlmostEqual(check["equity"], 0.0)
        self.assertAlmostEqual(cb.assets, 37.0)
        self.assertAlmostEqual(cb.liabilities, 37.0)
        self.assertAlmostEqual(cb.liquidity, 37.0)

    def test_qe_increases_bonds_and_reserves_without_breaking_identity(self):
        cb = pol.CentralBankBalance(bonds=30.0, fx_reserves=5.0, loans=2.0,
                                    currency=10.0, reserves=27.0)
        after = cb.buy_bonds(5.0)
        self.assertAlmostEqual(after.bonds, cb.bonds + 5.0)
        self.assertAlmostEqual(after.reserves, cb.reserves + 5.0)
        self.assertAlmostEqual(after.liquidity, cb.liquidity + 5.0)
        self.assertAlmostEqual(after.equity, cb.equity)
        self.assertTrue(after.check()["ok"])
        restored = after.sell_bonds(5.0)
        self.assertAlmostEqual(restored.bonds, cb.bonds)
        self.assertAlmostEqual(restored.reserves, cb.reserves)
        with self.assertRaises(pol.PolicyError):
            cb.sell_bonds(1000.0)

    def test_inconsistent_equity_is_rejected(self):
        with self.assertRaises(pol.PolicyError):
            pol.CentralBankBalance(bonds=30.0, fx_reserves=5.0, loans=2.0,
                                   currency=10.0, reserves=27.0, equity=5.0)


class DebtTests(unittest.TestCase):
    def test_debt_path_matches_the_project_identity(self):
        result = pol.project_debt(debt_ratio=0.55, real_rate=0.02, growth=0.015,
                                  primary_balance=0.01, periods=10)
        reference = reference_debt_path(0.55, 0.02, 0.015, 0.01, periods=10)
        self.assertEqual(len(result["debt_path"]), 11)
        for mine, expected in zip(result["debt_path"], reference):
            self.assertAlmostEqual(mine, float(expected), places=12)
        self.assertLess(result["max_debt_identity_residual"], 1e-12)

    def test_deficit_decomposition_identity(self):
        result = pol.project_debt(debt_ratio=0.6, real_rate=0.03, growth=0.01,
                                  primary_balance=-0.02, periods=8)
        debt = result["debt_path"]
        for index in range(8):
            interest = 0.03 * debt[index]
            deficit = interest - (-0.02)
            self.assertAlmostEqual(result["deficit_path"][index], deficit, places=12)
            self.assertAlmostEqual(result["interest_path"][index], interest, places=12)
            snowball = (0.03 - 0.01) / 1.01 * debt[index]
            self.assertAlmostEqual(debt[index + 1] - debt[index], snowball + 0.02, places=12)
        self.assertLess(result["max_deficit_identity_residual"], 1e-12)

    def test_r_g_sign_and_stabilising_balance(self):
        growing = pol.project_debt(0.55, 0.03, 0.01, 0.0, periods=10)
        self.assertEqual(growing["trend"], "GROWING")
        self.assertGreater(growing["stabilizing_primary_balance"], 0.0)
        declining = pol.project_debt(0.55, 0.005, 0.02, 0.0, periods=10)
        self.assertEqual(declining["trend"], "DECLINING")
        self.assertLess(declining["stabilizing_primary_balance"], 0.0)

    def test_deficit_shock_raises_debt_and_breach_is_reported(self):
        base = pol.project_debt(0.55, 0.02, 0.015, 0.0, periods=12)
        shocked = pol.project_debt(0.55, 0.02, 0.015, 0.0, periods=12, deficit_shock=0.02)
        self.assertGreater(shocked["final_debt_ratio"], base["final_debt_ratio"])
        limit = pol.project_debt(0.55, 0.02, 0.015, 0.0, periods=12, deficit_shock=0.02,
                                 debt_limit=0.6)
        self.assertTrue(limit["debt_limit_breached"])

    def test_invalid_debt_inputs_raise(self):
        with self.assertRaises(pol.PolicyError):
            pol.project_debt(growth=-1.0)
        with self.assertRaises(pol.PolicyError):
            pol.project_debt(debt_ratio=-0.1)
        with self.assertRaises(pol.PolicyError):
            pol.project_debt(periods=0)
        with self.assertRaises(pol.PolicyError):
            pol.project_debt(real_rate=float("nan"))


class AdapterAndSfcTests(unittest.TestCase):
    def test_t1_adapter_is_contiguous_and_persistent(self):
        path = pol.inflation_path_from_t1({"h1": 3.0, "h2": 2.5}, periods=4)
        self.assertEqual(path, (3.0, 2.5, 2.5, 2.5))
        self.assertEqual(pol.inflation_path_from_t1({1: 2.2}, periods=2), (2.2, 2.2))
        with self.assertRaises(pol.PolicyError):
            pol.inflation_path_from_t1({"h1": 3.0, "h3": 2.0}, periods=3)
        with self.assertRaises(pol.PolicyError):
            pol.inflation_path_from_t1({"h1": float("inf")}, periods=1)
        with self.assertRaises(pol.PolicyError):
            pol.inflation_path_from_t1({}, periods=1)

    def test_t3_adapter_is_monotone_and_bounded(self):
        self.assertEqual(pol.output_gap_from_recession_probability(0.2), 0.0)
        self.assertAlmostEqual(pol.output_gap_from_recession_probability(0.35), 0.0)
        self.assertAlmostEqual(pol.output_gap_from_recession_probability(0.675), -1.0)
        self.assertAlmostEqual(pol.output_gap_from_recession_probability(1.0), -2.0)
        with self.assertRaises(pol.PolicyError):
            pol.output_gap_from_recession_probability(1.5)

    def test_baseline_with_forecasts_uses_the_adapters(self):
        baseline = pol.PolicyBaseline(periods=3)
        adapted = baseline.with_t1_t3({"h1": 4.0, "h2": 3.0, "h3": 2.5}, 0.7)
        self.assertEqual(adapted.inflation_series(), (4.0, 3.0, 2.5))
        self.assertAlmostEqual(adapted.gap_series()[0],
                               pol.output_gap_from_recession_probability(0.7))

    def test_sfc_identity_and_fiscal_link(self):
        ok = pol.check_sfc_aggregates(0.20, 0.21, -0.01)
        self.assertTrue(ok["ok"])
        self.assertLess(abs(ok["residual"]), 1e-12)
        self.assertFalse(pol.check_sfc_aggregates(0.20, 0.21, 0.0)["ok"])
        with self.assertRaises(pol.PolicyError):
            pol.PolicyBaseline(savings_ratio=0.20, investment_ratio=0.22,
                               current_account_ratio=-0.01)
        report = by_id(fixture())["DEFICIT-PLUS-2PCT"]
        self.assertTrue(report["checks"]["fiscal_sfc_identity"])
        self.assertAlmostEqual(report["sfc"]["government_net_lending_ratio"],
                               -report["summary"]["final_deficit_ratio"])
        self.assertLess(abs(report["sfc"]["fiscal_residual"]), 1e-12)


class ScenarioTests(unittest.TestCase):
    def test_three_mvp_scenarios_are_present(self):
        result = fixture()
        self.assertEqual([entry["id"] for entry in result["index"]["scenarios"]],
                         ["RATE-PLUS-100BP", "DEFICIT-PLUS-2PCT", "TARIFF-INFLATION-SHOCK"])
        for report in result["reports"]:
            self.assertEqual(report["schema"], pol.SCENARIO_SCHEMA)
            self.assertEqual(report["status"], pol.STATUS)
            self.assertIs(report["admission"], False)
            self.assertIs(report["gating"], False)
            self.assertIs(report["causal_claim"], False)
            self.assertTrue(all(report["checks"][name] for name in
                                ("debt_identity", "deficit_identity", "central_bank_identity",
                                 "sfc_identity", "fiscal_sfc_identity")))

    def test_rate_plus_100bp_signs(self):
        report = by_id(fixture())["RATE-PLUS-100BP"]
        summary = report["summary"]
        self.assertGreater(summary["final_policy_rate_delta_pp"], 0.2)
        self.assertTrue(all(row["delta_policy_rate_pp"] >= 0.0 for row in report["paths"]))
        self.assertLess(summary["final_inflation_delta_pp"], 0.0)
        self.assertLess(summary["final_output_gap_delta_pp"], 0.0)
        self.assertGreater(summary["final_debt_ratio_delta"], 0.0)
        self.assertGreater(summary["final_deficit_ratio_delta"], 0.0)

    def test_deficit_plus_2pct_signs(self):
        report = by_id(fixture())["DEFICIT-PLUS-2PCT"]
        summary = report["summary"]
        self.assertGreater(summary["final_deficit_ratio_delta"], 0.02)
        self.assertGreater(summary["final_debt_ratio_delta"], 0.20)
        self.assertGreater(summary["max_inflation_delta_pp"], 0.0)
        self.assertGreater(summary["final_output_gap_delta_pp"], 0.0)
        self.assertGreater(summary["cumulative_deficit_ratio"], 0.20)

    def test_tariff_inflation_shock_signs_and_decay(self):
        report = by_id(fixture())["TARIFF-INFLATION-SHOCK"]
        summary = report["summary"]
        first = report["paths"][0]
        self.assertAlmostEqual(first["delta_inflation_pp"], 1.5)
        # The second period carries the 0.75 impulse plus a small gap-feedback term.
        self.assertGreater(report["paths"][1]["delta_inflation_pp"], 0.70)
        self.assertLess(report["paths"][1]["delta_inflation_pp"], 0.75)
        self.assertGreater(summary["max_inflation_delta_pp"], 1.0)
        self.assertGreater(summary["policy_rate_max_delta_pp"], 0.5)
        self.assertLess(summary["min_output_gap_delta_pp"], -0.2)
        self.assertLess(abs(summary["final_inflation_delta_pp"]), 0.05)
        self.assertGreater(summary["final_debt_ratio_delta"], 0.0)

    def test_identities_have_zero_residual(self):
        for report in fixture()["reports"]:
            self.assertLessEqual(report["summary"]["debt_identity_max_residual"], 1e-12)
            self.assertLessEqual(report["summary"]["deficit_identity_max_residual"], 1e-12)
            self.assertTrue(report["central_bank"]["check"]["ok"])
            self.assertTrue(report["central_bank"]["qe_illustration"]["identity_ok"])

    def test_qe_illustration_changes_composition_not_identity(self):
        report = fixture()["reports"][0]
        before = report["central_bank"]["baseline"]
        after = report["central_bank"]["qe_illustration"]["after"]
        self.assertAlmostEqual(after["bonds"] - before["bonds"],
                               after["reserves"] - before["reserves"])
        self.assertGreater(after["liquidity"], before["liquidity"])
        self.assertAlmostEqual(after["equity"], before["equity"])


class DeterminismAndHygieneTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self):
        first = pol.run_all()
        second = pol.run_all()
        self.assertEqual(pol.canonical_json(first), pol.canonical_json(second))
        self.assertEqual(first["index"]["digest"], second["index"]["digest"])

    def test_digest_tracks_a_scenario_parameter(self):
        baseline = pol.PolicyBaseline()
        scenario = pol.default_scenarios()[0]
        original = pol.run_all(baseline=baseline, scenarios=(scenario,))
        changed = pol.run_all(baseline=baseline,
                              scenarios=(replace(scenario, size=2.0),))
        self.assertNotEqual(original["index"]["digest"], changed["index"]["digest"])

    def test_only_selects_a_subset(self):
        result = pol.run_all(only=("RATE-PLUS-100BP",))
        self.assertEqual([entry["id"] for entry in result["index"]["scenarios"]],
                         ["RATE-PLUS-100BP"])
        with self.assertRaises(pol.PolicyError):
            pol.run_all(only=("NO-SUCH-SCENARIO",))

    def test_reports_contain_no_raw_data_and_no_non_finite_numbers(self):
        result = fixture()
        self.assertEqual(forbidden_keys(result), [])
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("NaN", payload)
        self.assertNotIn("Infinity", payload)
        for report in result["reports"]:
            self.assertEqual(set(report["paths"][0]),
                             {"period", "inflation_pp", "output_gap_pp", "policy_rate_pp",
                              "rule_rate_pp", "real_rate_pp", "growth", "primary_balance",
                              "deficit_ratio", "interest_burden_ratio", "debt_ratio",
                              "delta_inflation_pp", "delta_output_gap_pp",
                              "delta_policy_rate_pp", "delta_debt_ratio",
                              "delta_deficit_ratio"})

    def test_protocol_is_referenced_but_unchanged(self):
        reference = pol.PROTOCOL_REFERENCE
        document = json.loads(PROTOCOL.read_text(encoding="utf-8"))
        self.assertEqual(document["protocol_id"], reference["protocol_id"])
        self.assertEqual(document["version"], reference["version"])
        self.assertEqual(document["status"], reference["status"])
        self.assertIs(reference["admission"], False)
        expected = PROTOCOL_HASH.read_text(encoding="utf-8").split()[0]
        normalised = PROTOCOL.read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(normalised).hexdigest(), expected)

    def test_admission_is_false_everywhere(self):
        result = fixture()
        self.assertIs(result["index"]["admission"], False)
        self.assertIs(result["index"]["gating"], False)
        self.assertIs(result["index"]["causal_claim"], False)
        self.assertIn("не допуск", result["index"]["status_note"])
        for report in result["reports"]:
            self.assertIs(report["admission"], False)
            self.assertIs(report["gating"], False)
            self.assertIs(report["causal_claim"], False)
            self.assertIn("не допуск", report["status_note"])


class CliFailClosedTests(unittest.TestCase):
    def test_missing_config_is_blocked_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code = cli.main(["--config", str(Path(tmp) / "absent.json"), "--out", str(out)])
            self.assertEqual(code, cli.EXIT_BLOCKED)
            self.assertFalse(out.exists())

    def test_invalid_json_exits_1_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            out = Path(tmp) / "out"
            code = cli.main(["--config", str(path), "--out", str(out)])
            self.assertEqual(code, cli.EXIT_INVALID)
            self.assertFalse(out.exists())

    def test_invalid_baseline_exits_1_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad_baseline.json"
            path.write_text(json.dumps({"baseline": {
                "savings_ratio": 0.20, "investment_ratio": 0.22,
                "current_account_ratio": -0.01}}), encoding="utf-8")
            out = Path(tmp) / "out"
            code = cli.main(["--config", str(path), "--out", str(out)])
            self.assertEqual(code, cli.EXIT_INVALID)
            self.assertFalse(out.exists())

    def test_unknown_only_exits_1_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code = cli.main(["--only", "NO-SUCH-SCENARIO", "--out", str(out)])
            self.assertEqual(code, cli.EXIT_INVALID)
            self.assertFalse(out.exists())

    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "policy"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--periods", "6"])
            self.assertEqual(code, cli.EXIT_OK)
            report_json = out / "report.json"
            report_md = out / "report.md"
            self.assertTrue(report_json.is_file())
            self.assertTrue(report_md.is_file())
            self.assertTrue((out / "reports" / "RATE-PLUS-100BP.json").is_file())
            self.assertTrue((out / "reports" / "DEFICIT-PLUS-2PCT.md").is_file())
            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], cli.REPORT_SCHEMA)
            self.assertEqual(payload["status"], pol.STATUS)
            self.assertEqual(payload["index"]["baseline"]["periods"], 6)
            self.assertEqual(len(payload["scenarios"]), 3)
            self.assertTrue(all(len(report["paths"]) == 6 for report in payload["scenarios"]))
            text = report_md.read_text(encoding="utf-8")
            self.assertIn(pol.STATUS, text)
            self.assertIn("не допуск", text)
            self.assertIn("Допущения", text)

    def test_cli_json_flag_is_console_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "policy"
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--only", "RATE-PLUS-100BP", "--json"])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["schema"], pol.INDEX_SCHEMA)
            self.assertEqual(len(payload["scenarios"]), 1)


class BoundaryTests(unittest.TestCase):
    def test_scenario_boundaries(self):
        with self.assertRaises(pol.PolicyError):
            pol.PolicyScenario("BAD-ID", "x", "rate_shock", 0.0)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyScenario("OK-ID", "x", "rate_shock", 1.0, decay=0.0)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyScenario("OK-ID", "x", "rate_shock", 11.0)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyScenario("OK-ID", "x", "deficit_shock", 0.25)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyScenario("OK-ID", "x", "unknown", 1.0)

    def test_baseline_boundaries(self):
        with self.assertRaises(pol.PolicyError):
            pol.PolicyBaseline(periods=0)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyBaseline(periods=pol.MAX_PERIODS + 1)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyBaseline(potential_growth=-1.0)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyBaseline(primary_balance=2.0)
        with self.assertRaises(pol.PolicyError):
            pol.PolicyBaseline(cb_bonds_ratio=-0.1)
        with self.assertRaises(pol.PolicyError):
            pol.baseline_from_config({"unknown_field": 1})


if __name__ == "__main__":
    unittest.main()
