"""Tests for the stage-5 dynamic mechanisms (``src/amfe/dynamics``).

The suite is organised around the docs/38 (I10) contract - five epistemic kinds with different
admission rules - and then around the behaviour of each prototype:

* classification: identity / physical limit / institutional rule / hypothesis / scenario;
* Markov switching: transition validation, stationary distribution, durations, simulation, filter;
* expectations: adaptive rule, rational-expectations benchmark, RLS learning convergence;
* policy: Taylor rule as hypothesis, inflation-target institutional rule, NK determinacy;
* stability: Jacobian eigenvalues, stable/unstable/saddle, numerical Jacobian, explicit caveat;
* debt: the r-g identity, r<g vs r>g, stabilising primary balance, stress scenario;
* geopolitics: exogenous scenario with assumed loadings, event decay, no causal claim;
* diffusion: logistic/Bass S-curves, capacity limit, validation of parameters.

Run from the repository root: ``python -m unittest tests.test_dynamics -v``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import amfe.dynamics as dyn  # noqa: E402
from amfe.dynamics.base import (MECHANISM_KINDS, DynamicsError, Mechanism, Scenario,  # noqa: E402
                                validate_catalog)


class Classification(unittest.TestCase):
    """docs/38 (I10): the five kinds have different admission rules."""

    def test_valid_mechanism_and_kind_labels(self):
        mechanism = Mechanism(mechanism_id="T-ID", title_ru="Тест", kind=dyn.KIND_IDENTITY,
                              description_ru="тождество")
        self.assertEqual(mechanism.label_ru, "тождество")
        self.assertFalse(mechanism.falsifiable)
        self.assertIn("жёсткое ограничение", mechanism.admission_rule_ru())
        self.assertEqual(set(dyn.KIND_LABELS_RU), set(MECHANISM_KINDS))

    def test_invalid_kind_and_hypothesis_without_tests_are_rejected(self):
        with self.assertRaises(DynamicsError):
            Mechanism(mechanism_id="T-BAD", title_ru="x", kind="LAW", description_ru="x")
        with self.assertRaises(DynamicsError):
            Mechanism(mechanism_id="T-HYP", title_ru="x", kind=dyn.KIND_HYPOTHESIS,
                      description_ru="x")
        hypothesis = Mechanism(mechanism_id="T-HYP", title_ru="x", kind=dyn.KIND_HYPOTHESIS,
                               description_ru="x", tested_on=("tests/test_dynamics.py",))
        self.assertTrue(hypothesis.falsifiable)

    def test_scenarios_cannot_claim_causality_or_gate(self):
        scenario = Scenario(scenario_id="S", title_ru="шок", shocks={"x": 1.0}, horizon=3)
        self.assertEqual(scenario.path()["x"], [1.0, 1.0, 1.0])
        self.assertEqual(scenario.mechanism().kind, dyn.KIND_SCENARIO)
        with self.assertRaises(DynamicsError):
            Scenario(scenario_id="S", title_ru="шок", shocks={"x": 1.0}, horizon=3,
                     causal_claim=True)
        with self.assertRaises(DynamicsError):
            Scenario(scenario_id="S", title_ru="шок", shocks={}, horizon=3)

    def test_catalog_covers_all_kinds_and_validates(self):
        counts = dyn.counts()
        self.assertEqual(set(counts), set(MECHANISM_KINDS))
        for kind in MECHANISM_KINDS:
            self.assertGreaterEqual(counts[kind], 1, kind)
        self.assertEqual(len(dyn.MECHANISM_CATALOG), len(dyn.catalog()))
        for entry in dyn.catalog():
            self.assertIn(entry["kind"], MECHANISM_KINDS)
            self.assertTrue(entry["admission_rule_ru"])
            if entry["kind"] == dyn.KIND_HYPOTHESIS:
                self.assertTrue(entry["falsifiable"])
                self.assertTrue(entry["tested_on"])
            if entry["kind"] == dyn.KIND_SCENARIO:
                self.assertFalse(entry["causal_claim"])
                self.assertFalse(entry["gating"])
        self.assertEqual(dyn.mechanism_by_id("DYN-DEBT-IDENTITY").kind, dyn.KIND_IDENTITY)
        with self.assertRaises(KeyError):
            dyn.mechanism_by_id("NOPE")

    def test_duplicate_ids_rejected(self):
        first = Mechanism(mechanism_id="DUP", title_ru="a", kind=dyn.KIND_IDENTITY,
                          description_ru="a")
        with self.assertRaises(DynamicsError):
            validate_catalog((first, first))


class Regimes(unittest.TestCase):
    @staticmethod
    def model(seed=1):
        return dyn.MarkovSwitchingModel(np.array([[0.90, 0.10], [0.20, 0.80]]),
                                        means=[-1.0, 1.5], sigmas=[0.5, 0.7], seed=seed)

    def test_transition_validation(self):
        with self.assertRaises(DynamicsError):
            dyn.MarkovSwitchingModel(np.array([[0.5, 0.4], [0.2, 0.8]]), [0.0, 1.0], [1.0, 1.0])
        with self.assertRaises(DynamicsError):
            dyn.MarkovSwitchingModel(np.array([[0.5, 0.5], [0.2, 0.8]]), [0.0], [1.0, 1.0])
        with self.assertRaises(DynamicsError):
            dyn.MarkovSwitchingModel(np.array([[1.0]]), [0.0], [1.0])
        with self.assertRaises(DynamicsError):
            dyn.MarkovSwitchingModel(np.array([[0.5, 0.5], [0.2, 0.8]]), [0.0, 1.0], [1.0, -1.0])

    def test_stationary_distribution_and_durations(self):
        model = self.model()
        stationary = model.stationary_distribution()
        self.assertAlmostEqual(float(stationary.sum()), 1.0, places=12)
        np.testing.assert_allclose(stationary @ model.transition_matrix, stationary, atol=1e-12)
        self.assertAlmostEqual(stationary[0], 2.0 / 3.0, places=12)
        np.testing.assert_allclose(model.expected_durations(), [10.0, 5.0], atol=1e-12)
        absorbing = dyn.MarkovSwitchingModel(np.array([[1.0, 0.0], [0.5, 0.5]]),
                                             [0.0, 1.0], [1.0, 1.0])
        self.assertTrue(np.isinf(absorbing.expected_durations()[0]))

    def test_simulation_is_deterministic_and_matches_the_stationary_frequency(self):
        model = self.model()
        first = model.simulate(10000, seed=11)
        second = model.simulate(10000, seed=11)
        np.testing.assert_array_equal(first["states"], second["states"])
        np.testing.assert_array_equal(first["observations"], second["observations"])
        frequencies = np.bincount(first["states"], minlength=2) / first["states"].size
        np.testing.assert_allclose(frequencies, model.stationary_distribution(), atol=0.03)

    def test_hamilton_filter_probabilities(self):
        model = self.model()
        simulation = model.simulate(600, seed=2)
        filtered = model.filter(simulation["observations"])["filtered_probabilities"]
        self.assertEqual(filtered.shape, (600, 2))
        np.testing.assert_allclose(filtered.sum(axis=1), 1.0, atol=1e-12)
        high_observations = np.full(200, 1.5)
        posterior = model.regime_probabilities(high_observations)
        self.assertEqual(int(np.argmax(posterior[-1])), 1)
        self.assertGreater(float(posterior[-1, 1]), 0.99)
        self.assertTrue(np.isfinite(model.filter(simulation["observations"])["log_likelihood"]))
        self.assertEqual(int(np.argmax(posterior[-1])),
                         int(model.most_likely_regime(high_observations)[-1]))

    def test_mechanism_is_a_falsifiable_hypothesis(self):
        mechanism = dyn.MARKOV_SWITCHING_MECHANISM
        self.assertEqual(mechanism.kind, dyn.KIND_HYPOTHESIS)
        self.assertTrue(mechanism.falsifiable)


class Expectations(unittest.TestCase):
    def test_adaptive_expectations_converge_to_a_constant(self):
        rule = dyn.AdaptiveExpectations(gain=0.5, initial=0.0)
        result = rule.run(np.full(40, 10.0))
        self.assertLess(abs(result["final_forecast"] - 10.0), 1e-6)
        self.assertGreater(abs(result["errors"][0]), abs(result["errors"][-1]))
        with self.assertRaises(DynamicsError):
            dyn.AdaptiveExpectations(gain=0.0)
        with self.assertRaises(DynamicsError):
            dyn.AdaptiveExpectations(gain=1.5)
        with self.assertRaises(DynamicsError):
            rule.run(np.array([]))

    def test_rational_expectations_benchmark(self):
        process = dyn.RationalExpectationsAR1(intercept=1.0, phi=0.5, sigma=0.1)
        self.assertAlmostEqual(process.unconditional_mean, 2.0)
        self.assertAlmostEqual(process.forecast(4.0), 1.0 + 0.5 * 4.0)
        simulated = process.simulate(3000, seed=5)
        self.assertEqual(simulated.shape, (3000,))
        self.assertLess(abs(float(simulated.mean()) - process.unconditional_mean), 0.2)
        with self.assertRaises(DynamicsError):
            dyn.RationalExpectationsAR1(phi=1.0)
        with self.assertRaises(DynamicsError):
            process.forecast(float("nan"))
        with self.assertRaises(DynamicsError):
            process.simulate(0)

    def test_recursive_least_squares_learning_converges(self):
        learning = dyn.RecursiveLeastSquaresLearning(true_intercept=0.5, true_phi=0.6)
        result = learning.simulate_and_learn(3000, seed=7)
        self.assertTrue(result["converged"], result)
        self.assertLess(abs(result["final_phi"] - 0.6), 0.05)
        self.assertLess(abs(result["final_intercept"] - 0.5), 0.1)
        short = learning.run(np.array([0.1, 0.2, 0.3]))
        self.assertFalse(short["converged"])
        with self.assertRaises(DynamicsError):
            learning.run(np.array([0.1, 0.2]))
        with self.assertRaises(DynamicsError):
            dyn.RecursiveLeastSquaresLearning(true_phi=1.0)


class Policy(unittest.TestCase):
    def test_taylor_rule_as_a_hypothesis(self):
        rule = dyn.TaylorRule(r_star=0.5, pi_target=2.0, phi_pi=1.5, phi_y=0.5)
        self.assertAlmostEqual(rule.neutral_rate, 2.5)
        self.assertAlmostEqual(rule.rate(2.0, 0.0), 2.5)
        self.assertAlmostEqual(rule.rate(4.0, 0.0), 0.5 + 4.0 + 1.5 * 2.0)
        self.assertTrue(rule.taylor_principle)
        self.assertFalse(dyn.TaylorRule(phi_pi=0.5).taylor_principle)
        self.assertEqual(rule.mechanism().kind, dyn.KIND_HYPOTHESIS)
        with self.assertRaises(DynamicsError):
            dyn.TaylorRule(phi_pi=0.0)
        with self.assertRaises(DynamicsError):
            dyn.TaylorRule(phi_y=-0.5)
        with self.assertRaises(DynamicsError):
            rule.rate(float("inf"), 0.0)

    def test_inflation_target_is_an_institutional_rule(self):
        regime = dyn.InflationTargetRegime(target=2.0, source="protocol v4 / target statement")
        mechanism = regime.mechanism()
        self.assertEqual(mechanism.kind, dyn.KIND_INSTITUTIONAL_RULE)
        self.assertIn("2.0", mechanism.description_ru)
        with self.assertRaises(DynamicsError):
            dyn.InflationTargetRegime(target=2.0, source="")

    def test_new_keynesian_determinacy_follows_the_taylor_principle(self):
        active = dyn.NewKeynesianDeterminacySystem(rule=dyn.TaylorRule(phi_pi=1.5))
        passive = dyn.NewKeynesianDeterminacySystem(rule=dyn.TaylorRule(phi_pi=0.5))
        active_report = active.determinacy()
        passive_report = passive.determinacy()
        self.assertEqual(active_report["classification"], "DETERMINATE")
        self.assertTrue(active_report["determinate"])
        self.assertEqual(active_report["n_outside_unit_circle"], 2)
        self.assertEqual(passive_report["classification"], "INDETERMINATE")
        self.assertEqual(passive_report["n_inside_unit_circle"], 1)
        self.assertIn("Blanchard-Kahn", passive_report["criterion"])
        self.assertIn("локальной", passive_report["caveat"])
        matrix = active.expectational_matrix()
        self.assertEqual(matrix.shape, (2, 2))
        with self.assertRaises(DynamicsError):
            dyn.NewKeynesianDeterminacySystem(beta=1.0)
        with self.assertRaises(DynamicsError):
            dyn.NewKeynesianDeterminacySystem(kappa=-0.1)


class Stability(unittest.TestCase):
    def test_discrete_stable_and_unstable(self):
        stable = dyn.analyse_jacobian(np.diag([0.5, -0.2]))
        self.assertTrue(stable["stable"])
        self.assertEqual(stable["classification"], "STABLE")
        self.assertAlmostEqual(stable["spectral_radius"], 0.5)
        self.assertGreater(stable["margin"], 0.0)
        unstable = dyn.analyse_jacobian(np.diag([1.2, 0.3]))
        self.assertFalse(unstable["stable"])
        self.assertEqual(unstable["classification"], "UNSTABLE")
        marginal = dyn.analyse_jacobian(np.diag([1.0, 0.5]))
        self.assertFalse(marginal["stable"])
        self.assertEqual(marginal["classification"], "MARGINAL")
        self.assertIn("кризис", stable["caveat"])

    def test_continuous_saddle_and_stable(self):
        saddle = dyn.analyse_jacobian(np.diag([0.5, -0.5]), discrete=False)
        self.assertEqual(saddle["classification"], "SADDLE")
        self.assertFalse(saddle["stable"])
        stable = dyn.analyse_jacobian(np.diag([-1.0, -2.0]), discrete=False)
        self.assertEqual(stable["classification"], "STABLE")
        self.assertAlmostEqual(stable["margin"], 1.0)
        unstable = dyn.analyse_jacobian(np.diag([1.0, 2.0]), discrete=False)
        self.assertEqual(unstable["classification"], "UNSTABLE")

    def test_numerical_jacobian_and_system_analysis(self):
        def system(point):
            x, y = point
            return np.array([x * x + y, 0.5 * np.sin(y)])

        jacobian = dyn.numerical_jacobian(system, np.array([1.0, 0.0]))
        expected = np.array([[2.0, 1.0], [0.0, 0.5]])
        np.testing.assert_allclose(jacobian, expected, atol=1e-6)
        report = dyn.analyse_system(system, np.array([1.0, 0.0]))
        self.assertEqual(report["classification"], "UNSTABLE")
        self.assertEqual(report["point"], [1.0, 0.0])
        self.assertIn("caveat", report)
        with self.assertRaises(DynamicsError):
            dyn.numerical_jacobian(system, np.array([1.0, 0.0]), epsilon=0.0)
        with self.assertRaises(DynamicsError):
            dyn.analyse_jacobian(np.zeros((2, 3)))

    def test_linear_system_trajectories(self):
        converging = dyn.simulate_linear(np.diag([0.5, 0.5]), [1.0, -1.0], 20)
        self.assertLess(converging["max_abs_final"], 1e-3)
        diverging = dyn.simulate_linear(np.diag([1.5, 0.5]), [1.0, 1.0], 20)
        self.assertGreater(diverging["max_abs_final"], 1.0)
        with self.assertRaises(DynamicsError):
            dyn.simulate_linear(np.diag([0.5]), [1.0, 2.0], 5)


class Debt(unittest.TestCase):
    def test_debt_identity_and_stabilizing_balance(self):
        expected = (1.0 + 0.02) / (1.0 + 0.03) * 1.0 - 0.01
        self.assertAlmostEqual(dyn.debt_step(1.0, 0.02, 0.03, 0.01), expected)
        stabilizing = dyn.stabilizing_primary_balance(1.0, 0.02, 0.03)
        path = dyn.debt_path(1.0, 0.02, 0.03, stabilizing, periods=10)
        np.testing.assert_allclose(path, 1.0, atol=1e-12)
        with self.assertRaises(DynamicsError):
            dyn.debt_step(1.0, 0.02, -1.0, 0.0)

    def test_r_less_g_declines_and_r_greater_g_grows(self):
        favourable = dyn.sustainability(1.0, 0.01, 0.03, 0.0, periods=20)
        self.assertTrue(favourable["r_less_g"])
        self.assertEqual(favourable["trend"], "DECLINING")
        self.assertLess(favourable["final_debt"], favourable["initial_debt"])
        unfavourable = dyn.sustainability(1.0, 0.05, 0.02, 0.0, periods=20)
        self.assertFalse(unfavourable["r_less_g"])
        self.assertEqual(unfavourable["trend"], "GROWING")
        self.assertGreater(unfavourable["final_debt"], unfavourable["initial_debt"])
        self.assertEqual(unfavourable["mechanism"]["kind"], dyn.KIND_IDENTITY)
        with self.assertRaises(DynamicsError):
            dyn.sustainability(1.0, 0.01, 0.02, 0.0, periods=0)

    def test_balance_gap_and_stable_case(self):
        required = dyn.stabilizing_primary_balance(1.0, 0.02, 0.03)
        report = dyn.sustainability(1.0, 0.02, 0.03, required)
        self.assertEqual(report["trend"], "STABLE")
        self.assertAlmostEqual(report["balance_gap"], 0.0, places=12)
        easier = dyn.sustainability(1.0, 0.02, 0.03, required + 0.01)
        self.assertLess(easier["final_debt"], 1.0)
        self.assertGreater(easier["balance_gap"], 0.0)

    def test_rg_stress_scenario_is_exogenous(self):
        stress = dyn.debt_stress_scenario(1.0, 0.02, 0.03, 0.0, rate_shock=0.02,
                                          growth_shock=-0.01, periods=10)
        self.assertEqual(stress["scenario"]["kind"], dyn.KIND_SCENARIO)
        self.assertGreater(stress["stressed_final"], stress["baseline_final"])
        self.assertGreater(stress["final_gap"], 0.0)
        self.assertIn("не оценены как причинные", stress["caveat"])
        with self.assertRaises(DynamicsError):
            dyn.debt_stress_scenario(1.0, 0.02, 0.03, 0.0, rate_shock=float("nan"))


class Geopolitics(unittest.TestCase):
    def test_risk_scenario_effects_and_caveat(self):
        scenario = dyn.GeopoliticalRiskScenario(risk_index=0.5, persistence=0.5)
        effects = scenario.macro_effects(4)
        self.assertLess(effects["gdp_drag"][0], 0.0)
        self.assertGreater(effects["inflation_push"][0], 0.0)
        self.assertLess(abs(effects["gdp_drag"][-1]), abs(effects["gdp_drag"][0]))
        report = scenario.report(3)
        self.assertFalse(report["causal_claim"])
        self.assertIn("сценарий", report["caveat"])
        self.assertEqual(report["mechanism"]["kind"], dyn.KIND_SCENARIO)
        with self.assertRaises(DynamicsError):
            dyn.GeopoliticalRiskScenario(risk_index=1.5)
        with self.assertRaises(DynamicsError):
            dyn.GeopoliticalRiskScenario(persistence=-0.1)
        with self.assertRaises(DynamicsError):
            dyn.GeopoliticalRiskScenario(loadings={"unknown": 0.1})

    def test_event_driven_risk_index(self):
        index = dyn.risk_index_from_events([{"period": 0, "severity": 1.0}], decay=0.5,
                                           horizon=4)
        np.testing.assert_allclose(index, [1.0, 0.5, 0.25, 0.125])
        overlapping = dyn.risk_index_from_events(
            [{"period": 0, "severity": 1.0}, {"period": 1, "severity": 1.0}], decay=0.5,
            horizon=3)
        np.testing.assert_allclose(overlapping, [1.0, 1.0, 0.75])
        with self.assertRaises(DynamicsError):
            dyn.risk_index_from_events([{"period": 0, "severity": 2.0}])
        with self.assertRaises(DynamicsError):
            dyn.risk_index_from_events([])

    def test_scenario_comparison_orders_shocks(self):
        comparison = dyn.scenario_comparison(periods=10, risk_levels=(0.2, 0.5, 0.8))
        drags = [item["gdp_drag_total"] for item in comparison["scenarios"].values()]
        self.assertEqual(drags, sorted(drags, reverse=True))  # larger risk -> larger drag
        self.assertIn("не причинный эффект", comparison["caveat"])


class Diffusion(unittest.TestCase):
    def test_logistic_s_curve_is_bounded_and_monotone(self):
        diffusion = dyn.LogisticDiffusion(rate=0.4, ceiling=100.0, initial_fraction=1.0)
        report = diffusion.report(120)
        self.assertTrue(report["monotone"])
        self.assertTrue(report["bounded"])
        path = report["path"]
        self.assertLessEqual(float(path.max()), 100.0 * (1.0 + 1e-12))
        times = report["times_to_reach"]
        self.assertLess(times["fraction_0.10"], times["fraction_0.50"])
        self.assertLess(times["fraction_0.50"], times["fraction_0.90"])
        self.assertEqual(report["mechanism"]["kind"], dyn.KIND_HYPOTHESIS)
        self.assertEqual(report["physical_limit"]["kind"], dyn.KIND_PHYSICAL_LIMIT)
        self.assertAlmostEqual(report["inflection_level"], 50.0)

    def test_logistic_parameters_validation(self):
        with self.assertRaises(DynamicsError):
            dyn.LogisticDiffusion(rate=0.0)
        with self.assertRaises(DynamicsError):
            dyn.LogisticDiffusion(ceiling=-1.0)
        with self.assertRaises(DynamicsError):
            dyn.LogisticDiffusion(initial_fraction=1.5)
        with self.assertRaises(DynamicsError):
            dyn.LogisticDiffusion(initial_fraction=1.0).simulate(0)

    def test_bass_diffusion_peak_matches_analytic_time(self):
        report = dyn.diffusion_report(0.03, 0.40, periods=60)
        self.assertTrue(report["monotone"])
        self.assertTrue(report["bounded"])
        self.assertIsNotNone(report["analytic_peak_time"])
        self.assertLess(abs(report["peak_growth_period"] - report["analytic_peak_time"]), 2.0)
        path = dyn.bass_simulate(0.03, 0.40, ceiling=1.0, periods=10)
        self.assertEqual(path.size, 11)
        self.assertLessEqual(float(path.max()), 1.0 + 1e-12)
        self.assertIsNone(dyn.bass_peak_time(0.1, 0.1))
        with self.assertRaises(DynamicsError):
            dyn.bass_simulate(0.0, 0.2)
        with self.assertRaises(DynamicsError):
            dyn.bass_peak_time(0.1, -0.2)

    def test_physical_limit_cannot_be_exceeded_even_with_huge_rate(self):
        aggressive = dyn.LogisticDiffusion(rate=50.0, ceiling=1.0, initial_fraction=0.9)
        path = aggressive.simulate(50)
        self.assertTrue(np.all(path <= 1.0 + 1e-12))
        self.assertGreater(float(path[-1]), 0.99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
