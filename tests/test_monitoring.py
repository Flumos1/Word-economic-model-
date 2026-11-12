"""Stage 8-9 monitoring: versioning, degradation detectors, drift, contours, decisions and rollback.

The suite is synthetic by design: no admitted candidate exists yet (docs/76), so the monitoring layer is
tested on controlled series where the right answer is known.  Statistical detector tests use fixed
seeds and are split into two claims:

* no false alarm on stationary i.i.d. and AR(1) series with autocorrelation-robust calibration;
* detection of a persistent structural shift within a bounded number of observations.

The decision rule is cross-checked against the frozen admission harness (tools/evaluate.py) so that
monitoring can never apply a looser test than the protocol.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))  # frozen harness, used only for the cross-check test

from amfe.monitoring import (  # noqa: E402
    ContourCoordinator,
    DecisionError,
    DriftMonitor,
    FastContour,
    InverseMSEWeights,
    ModelVersion,
    MonitoringLoop,
    Observation,
    ObservationGate,
    RollbackController,
    RollingMetrics,
    SlowContour,
    SwitchRule,
    VersionError,
    VersionJournal,
    aggregate_loss,
    crps,
    cusum,
    detect_concept_drift,
    detect_data_drift,
    ewma,
    ewma_detector,
    hash_files,
    hash_source,
    load_threshold_table,
    long_run_sigma,
    make_version,
    mean_shift_test,
    one_sided_dm,
    page_hinkley,
    psi,
    relationship_drift,
    rolling_mae,
    rolling_rmse,
    strict_threshold,
)


def ar1(n: int, phi: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0.0, 1.0, size=n)
    values = np.zeros(n)
    for index in range(1, n):
        values[index] = phi * values[index - 1] + shocks[index]
    return values


def squared_error_series(n: int = 120, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 1.0, size=n) ** 2


class Versioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_identity_is_bound_to_code_and_weights(self):
        a = make_version("MODEL", {"w1": 0.5, "w2": 0.5}, code_source="def f(): return 1")
        b = make_version("MODEL", {"w1": 0.5, "w2": 0.5}, code_source="def f(): return 1")
        c = make_version("MODEL", {"w1": 0.7, "w2": 0.3}, code_source="def f(): return 1")
        d = make_version("MODEL", {"w1": 0.5, "w2": 0.5}, code_source="def f(): return 2")
        self.assertEqual(a.version_id, b.version_id)
        self.assertNotEqual(a.version_id, c.version_id)
        self.assertNotEqual(a.version_id, d.version_id)
        self.assertEqual(a.validate(), [])
        self.assertEqual({k: round(v, 6) for k, v in a.weights.items()}, {"w1": 0.5, "w2": 0.5})

    def test_code_hash_helpers(self):
        first = self.dir / "a.py"
        second = self.dir / "b.py"
        first.write_text("x = 1\r\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        self.assertEqual(hash_files([first, second]), hash_files([second, first]))
        self.assertNotEqual(hash_files([first]), hash_files([second]))
        self.assertNotEqual(hash_source("x = 1"), hash_source("x = 2"))

    def test_register_duplicates_and_idempotent_ensure(self):
        journal = VersionJournal()
        version = make_version("M", {"a": 1.0}, code_source="code")
        journal.register(version)
        with self.assertRaises(VersionError):
            journal.register(version)
        entry, created = journal.ensure_version(version)
        self.assertFalse(created)
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(len(journal.entries), 1)

    def test_switch_and_rollback_journal(self):
        journal = VersionJournal()
        a = make_version("M", {"w": 1.0}, code_source="A")
        b = make_version("M", {"w": 2.0}, code_source="B")
        journal.register(a)
        journal.register(b)
        self.assertIsNone(journal.active)
        journal.activate(a.version_id, "initial adoption")
        self.assertEqual(journal.active, a.version_id)
        journal.activate(b.version_id, "better version", observations=30)
        self.assertEqual(journal.active, b.version_id)
        journal.rollback("regression detected", observations=40)
        self.assertEqual(journal.active, a.version_id)
        switches = journal.switches()
        self.assertEqual([entry["kind"] for entry in switches], ["SWITCH", "SWITCH", "ROLLBACK"])
        self.assertEqual(switches[-1]["previous_active"], b.version_id)
        ok, problems = journal.verify()
        self.assertTrue(ok, problems)

    def test_rollback_to_explicit_target_and_guards(self):
        journal = VersionJournal()
        versions = [make_version("M", {"w": float(i)}, code_source=f"code{i}") for i in (1, 2, 3)]
        for version in versions:
            journal.register(version)
        journal.activate(versions[0].version_id, "initial")
        journal.activate(versions[1].version_id, "second")
        journal.activate(versions[2].version_id, "third")
        journal.rollback("explicit target", target_version_id=versions[0].version_id)
        self.assertEqual(journal.active, versions[0].version_id)
        with self.assertRaises(VersionError):
            journal.rollback("same version", target_version_id=versions[0].version_id)
        with self.assertRaises(VersionError):
            journal.rollback("unknown", target_version_id="missing")
        fresh = VersionJournal()
        with self.assertRaises(VersionError):
            fresh.rollback("nothing to roll back")

    def test_persistence_round_trip_and_tamper_detection(self):
        path = self.dir / "journal.jsonl"
        journal = VersionJournal(path)
        a = make_version("M", {"w": 1.0}, code_source="A")
        b = make_version("M", {"w": 1.0}, code_source="B")
        journal.register(a)
        journal.register(b)
        journal.activate(a.version_id, "initial")
        journal.activate(b.version_id, "upgrade", observations=20)
        reopened = VersionJournal(path)
        self.assertEqual(reopened.active, b.version_id)
        self.assertEqual(len(reopened.entries), len(journal.entries))
        self.assertTrue(reopened.verify()[0])
        lines = path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["kind"] = "NOTE"
        lines[0] = json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(VersionError):
            VersionJournal(path)

    def test_decision_and_update_records_are_audit_only(self):
        journal = VersionJournal()
        a = make_version("M", {"w": 1.0}, code_source="A")
        journal.register(a)
        journal.activate(a.version_id, "initial")
        before = journal.active
        journal.record_decision({"action": "HOLD", "passed": False,
                                 "candidate_version": "x", "active_version": a.version_id,
                                 "reasons": ["not significantly better"], "per_loss": {}})
        journal.record_update("SLOW_CONTOUR", {"weights": {"M": 1.0}})
        self.assertEqual(journal.active, before)
        kinds = [entry["kind"] for entry in journal.entries]
        self.assertEqual(kinds, ["VERSION", "SWITCH", "DECISION", "UPDATE"])
        self.assertTrue(journal.verify()[0])


class Metrics(unittest.TestCase):
    def test_rolling_metrics_hand_values(self):
        tracker = RollingMetrics(window=3)
        for actual, point in ((1.0, 0.0), (2.0, 2.0), (4.0, 1.0), (5.0, 4.0)):
            tracker.update(actual, point=point)
        snapshot = tracker.snapshot()
        # last three absolute errors: 0, 3, 1 -> MAE 4/3 ; squared: 0, 9, 1 -> RMSE sqrt(10/3)
        self.assertAlmostEqual(snapshot["MAE"], 4.0 / 3.0)
        self.assertAlmostEqual(snapshot["RMSE"], float(np.sqrt(10.0 / 3.0)))
        self.assertEqual(snapshot["MAE_n"], 3)
        tracker.update(1.0, probability=0.9)
        self.assertAlmostEqual(tracker.snapshot()["Brier"], 0.01)

    def test_rolling_helpers_and_crps_formula(self):
        errors = np.array([1.0, 2.0, 3.0, 4.0])
        self.assertTrue(np.isnan(rolling_mae(errors, 3)[0]))
        self.assertAlmostEqual(float(rolling_mae(errors, 3)[-1]), 3.0)
        self.assertAlmostEqual(float(rolling_rmse(errors, 2)[-1]), float(np.sqrt(12.5)))
        actual = np.array([0.1, -0.2, 0.4])
        quantile_levels = np.round(np.arange(0.05, 1.0, 0.05), 2)
        quantiles = actual[:, None] + np.linspace(-1.0, 1.0, 19)[None, :]
        manual = np.zeros(actual.size)
        for column, tau in enumerate(quantile_levels):
            diff = actual - quantiles[:, column]
            pinball = np.where(diff >= 0.0, tau * diff, (tau - 1.0) * diff)
            manual += pinball
        manual = 2.0 * manual / len(quantile_levels)
        np.testing.assert_allclose(crps(quantiles, actual), manual, atol=1e-12)

    def test_ewma_recursion_and_step_detection(self):
        values = np.array([0.0, 0.0, 10.0, 10.0])
        np.testing.assert_allclose(ewma(values, 0.5), [0.0, 0.0, 5.0, 7.5])
        step = np.concatenate([np.zeros(60), np.full(40, 5.0)])
        result = ewma_detector(step, alpha=0.2, target=0.0, sigma=1.0, threshold=3.0)
        self.assertTrue(result.detected)
        self.assertGreaterEqual(result.first_index, 59)
        self.assertTrue(np.all(np.isfinite(result.series)))

    def test_cusum_detects_persistent_shift(self):
        rng = np.random.default_rng(11)
        base = rng.normal(0.0, 1.0, size=200)
        upward = np.concatenate([base[:120], base[120:] + 1.5])
        result = cusum(upward, target=0.0, sigma=1.0, delta=1.0, threshold=10.0)
        self.assertTrue(result.detected)
        self.assertGreaterEqual(result.first_index, 100)
        downward = np.concatenate([base[:120], base[120:] - 1.5])
        self.assertTrue(cusum(downward, target=0.0, sigma=1.0, delta=1.0,
                              threshold=10.0).detected)

    def test_page_hinkley_direction(self):
        rng = np.random.default_rng(12)
        base = rng.normal(0.0, 1.0, size=200)
        upward = np.concatenate([base[:120], base[120:] + 1.5])
        up = page_hinkley(upward, target=0.0, sigma=1.0, threshold=3.5, direction="increase")
        down = page_hinkley(upward, target=0.0, sigma=1.0, threshold=3.5, direction="decrease")
        self.assertTrue(up.detected)
        self.assertFalse(down.detected)
        downward = np.concatenate([base[:120], base[120:] - 1.5])
        self.assertTrue(page_hinkley(downward, target=0.0, sigma=1.0, threshold=3.5,
                                     direction="decrease").detected)

    def test_long_run_sigma_accounts_for_autocorrelation(self):
        process = ar1(400, 0.5, seed=3)
        self.assertGreater(long_run_sigma(process), float(np.std(process, ddof=1)))

    def test_no_false_alarm_on_stationary_series(self):
        for label, generator in (
                ("iid", lambda seed: np.random.default_rng(3000 + seed).normal(0, 1, 300)),
                ("ar1", lambda seed: ar1(300, 0.5, 3000 + seed))):
            for seed in range(20):
                values = generator(seed)
                cusum_result = cusum(values, target=0.0, threshold=10.0)
                self.assertFalse(cusum_result.detected, f"{label}: CUSUM false alarm, seed {seed}")
                ph_result = page_hinkley(values, target=0.0, threshold=3.5)
                self.assertFalse(ph_result.detected, f"{label}: PH false alarm, seed {seed}")

    def test_no_false_alarm_ewma_on_autocorrelated_series(self):
        for seed in range(20):
            values = ar1(300, 0.5, 6000 + seed)
            self.assertFalse(ewma_detector(values, alpha=0.2, target=0.0, threshold=4.0).detected,
                             f"EWMA false alarm on AR(1), seed {seed}")


class Drift(unittest.TestCase):
    def test_ks_detects_location_shift_and_accepts_same_law(self):
        rng = np.random.default_rng(20)
        reference = rng.normal(0.0, 1.0, 300)
        same = rng.normal(0.0, 1.0, 300)
        shifted = rng.normal(1.5, 1.0, 300)
        self.assertFalse(detect_data_drift({"x": reference}, {"x": same},
                                           min_votes=3).drift_detected)
        report = detect_data_drift({"x": reference}, {"x": shifted}, min_votes=2)
        self.assertTrue(report.drift_detected)
        self.assertTrue(report.features["x"].methods["ks"]["drift"])
        self.assertGreaterEqual(report.features["x"].votes, 2)

    def test_psi_and_mean_shift(self):
        rng = np.random.default_rng(21)
        reference = rng.normal(0.0, 1.0, 500)
        unchanged = rng.permutation(reference)  # same distribution, same mean
        far = rng.normal(2.0, 1.0, 500)
        self.assertFalse(psi(reference, unchanged)["drift"])
        self.assertFalse(mean_shift_test(reference, unchanged)["drift"])
        self.assertTrue(psi(reference, far)["drift"])
        self.assertTrue(mean_shift_test(reference, far)["drift"])

    def test_drift_monitor_reports_and_rebaseline(self):
        rng = np.random.default_rng(22)
        monitor = DriftMonitor(reference_features={"x": rng.normal(0, 1, 200)},
                               reference_errors=rng.normal(0, 1, 200))
        report = monitor.check_features({"x": rng.normal(3.0, 1.0, 200)})
        self.assertTrue(report.drift_detected)
        self.assertEqual(len(monitor.reports), 1)
        monitor.shift_reference(features={"x": rng.normal(3.0, 1.0, 200)},
                                errors=rng.normal(0, 1, 200))
        self.assertFalse(monitor.check_features({"x": rng.normal(3.0, 1.0, 200)}).drift_detected)

    def test_concept_drift_from_errors(self):
        rng = np.random.default_rng(23)
        reference = rng.normal(0.0, 1.0, 300)
        self.assertFalse(detect_concept_drift(reference, rng.normal(0.0, 1.0, 300)).drift_detected)
        report = detect_concept_drift(reference, rng.normal(1.5, 1.0, 300))
        self.assertTrue(report.drift_detected)
        self.assertTrue(report.error_drift)

    def test_relationship_drift(self):
        rng = np.random.default_rng(24)
        x_reference = rng.normal(0, 1, 300)
        y_reference = 2.0 * x_reference + rng.normal(0, 0.2, 300)
        x_recent = rng.normal(0, 1, 300)
        y_recent = -2.0 * x_recent + rng.normal(0, 0.2, 300)
        report = relationship_drift(x_reference, y_reference, x_recent, y_recent)
        self.assertTrue(report.drift_detected)
        self.assertTrue(report.relation_drift)


class Decisions(unittest.TestCase):
    def setUp(self):
        self.base = squared_error_series(seed=31)

    def test_thresholds_come_from_the_frozen_table(self):
        table = load_threshold_table()
        self.assertEqual(len(table), 44)
        self.assertEqual(strict_threshold(table, "T1_CPI_YOY", 3, "RMSE"), 0.97)
        h12 = [value for (target, horizon, loss, window), value in table.items()
               if target == "T1_CPI_YOY" and horizon == 12 and loss == "CRPS"]
        self.assertEqual(strict_threshold(table, "T1_CPI_YOY", 12, "CRPS"), min(h12))
        rule = SwitchRule(threshold_override={"RMSE": 0.5})
        self.assertEqual(rule.threshold("RMSE"), 0.5)
        with self.assertRaises(DecisionError):
            strict_threshold(table, "T1_CPI_YOY", 99, "RMSE")

    def test_dm_mirrors_the_frozen_harness(self):
        import evaluate as harness  # frozen admission harness
        rng = np.random.default_rng(32)
        for seed in range(5):
            differential = rng.normal(-0.2, 1.0, size=120)
            for horizon in (1, 3):
                for alternative in ("less", "greater"):
                    ours = one_sided_dm(differential, horizon, alternative)
                    theirs = harness.dm_test(differential, horizon, alternative)
                    self.assertAlmostEqual(ours["p_value"], theirs["p_value"], places=12)
                    self.assertEqual(ours["hac_lags"], theirs["hac_lags"])
                    self.assertEqual(ours["hln_factor"], theirs["hln_factor"])

    def test_promotes_significantly_better_candidate(self):
        rule = SwitchRule(horizon=3)
        candidate = {loss: 0.49 * self.base for loss in ("RMSE", "CRPS")}
        active = {loss: self.base for loss in ("RMSE", "CRPS")}
        decision = rule.evaluate(candidate, active, target="T1_CPI_YOY")
        self.assertEqual(decision["action"], "PROMOTE")
        # RMSE is a root-mean-square: 0.49 * squared errors -> sqrt(0.49) = 0.7 relative RMSE
        self.assertAlmostEqual(decision["per_loss"]["RMSE"]["relative_loss"], 0.7)
        self.assertLess(decision["per_loss"]["RMSE"]["p_value"], 0.10)

    def test_identical_candidate_holds(self):
        rule = SwitchRule(horizon=1)
        candidate = {"RMSE": self.base}
        decision = rule.evaluate(candidate, {"RMSE": self.base})
        self.assertEqual(decision["action"], "HOLD")
        self.assertAlmostEqual(decision["per_loss"]["RMSE"]["relative_loss"], 1.0)

    def test_significantly_worse_candidate_is_rejected(self):
        rule = SwitchRule(horizon=3)
        decision = rule.evaluate({"RMSE": 2.25 * self.base}, {"RMSE": self.base})
        self.assertEqual(decision["action"], "REJECT")
        self.assertLess(decision["per_loss"]["RMSE"]["p_opposite"], 0.10)

    def test_no_update_without_observations(self):
        rule = SwitchRule()
        candidate = {"RMSE": 0.25 * self.base}
        decision = rule.evaluate(candidate, {"RMSE": self.base}, observations=0)
        self.assertEqual(decision["action"], "INSUFFICIENT_DATA")
        decision = rule.evaluate(candidate, {"RMSE": self.base}, observations=5)
        self.assertEqual(decision["action"], "INSUFFICIENT_DATA")
        self.assertEqual(decision["per_loss"], {})

    def test_controller_promotes_and_rolls_back(self):
        journal = VersionJournal()
        controller = RollbackController(journal, SwitchRule(horizon=3))
        old = make_version("ENSEMBLE", {"RW": 0.5, "AR_BIC": 0.5}, code_source="old code")
        new = make_version("ENSEMBLE", {"RW": 0.3, "AR_BIC": 0.7}, code_source="new code")
        journal.register(old)
        journal.activate(old.version_id, "initial adoption")
        active_losses = {loss: self.base for loss in ("RMSE", "CRPS")}
        candidate_losses = {loss: 0.49 * self.base for loss in ("RMSE", "CRPS")}
        result = controller.consider(new, journal.version(journal.active), candidate_losses,
                                     active_losses, target="T1_CPI_YOY")
        self.assertEqual(result["action"], "PROMOTE")
        self.assertEqual(journal.active, new.version_id)
        promoted_entries = len(journal.entries)
        # idempotent: the same candidate is already active -> NOOP, no new journal entries
        again = controller.consider(new, journal.version(journal.active), candidate_losses,
                                    active_losses, target="T1_CPI_YOY")
        self.assertEqual(again["action"], "NOOP")
        self.assertEqual(len(journal.entries), promoted_entries)
        # the promoted version degrades: the predecessor wins -> automatic rollback
        rollback = controller.auto_rollback_check(old, None, candidate_losses,
                                                  active_losses, target="T1_CPI_YOY")
        self.assertEqual(rollback["action"], "ROLLBACK")
        self.assertEqual(journal.active, old.version_id)
        kinds = [entry["kind"] for entry in journal.entries]
        self.assertIn("ROLLBACK", kinds)
        self.assertTrue(journal.verify()[0])

    def test_controller_keeps_active_when_candidate_is_not_better(self):
        journal = VersionJournal()
        controller = RollbackController(journal)
        old = make_version("M", {"w": 1.0}, code_source="old")
        same = make_version("M", {"w": 1.0}, code_source="same")
        journal.register(old)
        journal.activate(old.version_id, "initial")
        result = controller.consider(same, journal.version(journal.active),
                                     {"RMSE": self.base}, {"RMSE": self.base})
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(journal.active, old.version_id)
        # no switch entry was written for the held candidate
        self.assertFalse([entry for entry in journal.history("SWITCH")
                          if entry["active"] == same.version_id])

    def test_controller_does_not_promote_on_insufficient_observations(self):
        journal = VersionJournal()
        controller = RollbackController(journal)
        old = make_version("M", {"w": 1.0}, code_source="old")
        new = make_version("M", {"w": 2.0}, code_source="new")
        journal.register(old)
        journal.activate(old.version_id, "initial")
        result = controller.consider(new, journal.version(journal.active),
                                     {"RMSE": 0.25 * self.base}, {"RMSE": self.base},
                                     observations=0)
        self.assertEqual(result["action"], "INSUFFICIENT_DATA")
        self.assertEqual(journal.active, old.version_id)
        self.assertEqual(journal.history("SWITCH")[-1]["active"], old.version_id)


class Contours(unittest.TestCase):
    def test_gate_rejects_pseudo_real_time_and_contaminated(self):
        gate = ObservationGate()
        good = Observation(origin="2026-11", values={"x": 1.0},
                           vintage_hash="a" * 64)
        pseudo = Observation(origin="2026-11", values={"x": 1.0},
                             data_class="PSEUDO_REAL_TIME")
        contaminated = Observation(origin="2026-11", values={"x": 1.0},
                                   data_class="CONTAMINATED")
        self.assertTrue(gate.check(good)[0])
        self.assertFalse(gate.check(pseudo)[0])
        self.assertFalse(gate.check(contaminated)[0])
        rejected = Observation(origin="2026-11", values={"x": 1.0}, admissible=False,
                               reason="leakage")
        self.assertFalse(gate.check(rejected)[0])
        self.assertEqual(gate.accepted, 1)
        self.assertEqual(gate.rejected, 3)
        strict = ObservationGate(require_vintage_hash=True)
        self.assertFalse(strict.check(Observation(origin="2026-11", values={"x": 1.0}))[0])

    def test_fast_contour_updates_only_admissible_observations(self):
        contour = FastContour(initial_state={"level": 0.0})
        update = contour.update(Observation(origin="2026-11", values={"level": 10.0},
                                            data_class="PSEUDO_REAL_TIME"))
        self.assertFalse(update.updated)
        self.assertEqual(contour.state["level"], 0.0)
        update = contour.update(Observation(origin="2026-11", values={"level": 10.0},
                                            vintage_hash="b" * 64))
        self.assertTrue(update.updated)
        self.assertGreater(contour.state["level"], 0.0)
        self.assertLess(contour.state["level"], 10.0)  # Kalman gain shrinks the observation
        empty = FastContour()
        self.assertFalse(empty.update([]).updated)

    def test_slow_contour_requires_minimum_observations(self):
        contour = SlowContour(min_observations=12)
        frozen = contour.update({"A": [1.0] * 11, "B": [2.0] * 11}, observations=11)
        self.assertFalse(frozen.updated)
        self.assertEqual(contour.weights, {})
        updated = contour.update({"A": [1.0] * 12, "B": [2.0] * 12}, observations=12)
        self.assertTrue(updated.updated)
        self.assertGreater(updated.weights["A"], updated.weights["B"])
        self.assertAlmostEqual(sum(updated.weights.values()), 1.0)

    def test_slow_contour_idempotent_and_no_observations(self):
        contour = SlowContour()
        none = contour.update(None, observations=0)
        self.assertFalse(none.updated)
        history = {"A": [1.0] * 15, "B": [2.0] * 15}
        first = contour.update(history, observations=15)
        self.assertTrue(first.updated)
        weights = dict(contour.weights)
        second = contour.update(history, observations=15)
        self.assertFalse(second.updated)
        self.assertIn("no new observations", second.reason)
        self.assertEqual(contour.weights, weights)

    def test_inverse_mse_weights(self):
        updater = InverseMSEWeights()
        weights = updater.update({"A": np.full(12, 0.25), "B": np.full(12, 1.0)})
        self.assertGreater(weights["A"], weights["B"])
        self.assertAlmostEqual(weights["A"], 0.8)
        with self.assertRaises(Exception):
            updater.update({"A": np.array([1.0, -1.0])})

    def test_coordinator_blocks_all_updates_without_admissible_observations(self):
        coordinator = ContourCoordinator(slow_every=3)
        for step in range(10):
            report = coordinator.step(Observation(origin=f"2026-{step + 1:02d}",
                                                  values={"x": float(step)},
                                                  data_class="PSEUDO_REAL_TIME"),
                                      losses={"A": 1.0, "B": 2.0})
            self.assertFalse(report["admissible"])
        self.assertEqual(coordinator.admissible, 0)
        self.assertEqual(coordinator.loss_history, {})
        self.assertEqual(coordinator.fast.updates, 0)
        self.assertEqual(coordinator.slow.updates, 0)

    def test_coordinator_updates_and_journals_slow_contour(self):
        journal = VersionJournal()
        coordinator = ContourCoordinator(slow_every=3, journal=journal)
        for step in range(12):
            report = coordinator.step(Observation(origin=f"2026-{step + 1:02d}",
                                                  values={"x": 1.0},
                                                  vintage_hash="c" * 64),
                                      losses={"A": 1.0, "B": 2.0})
            self.assertTrue(report["admissible"])
        self.assertEqual(coordinator.admissible, 12)
        self.assertTrue(coordinator.slow.updates >= 1)
        self.assertTrue(coordinator.loss_history["A"])
        scopes = [entry.get("scope") for entry in journal.entries]
        self.assertIn("SLOW_CONTOUR", scopes)
        self.assertTrue(journal.verify()[0])


class MonitoringLoopEndToEnd(unittest.TestCase):
    def test_promote_then_automatic_rollback(self):
        journal = VersionJournal()
        loop = MonitoringLoop(journal, rule=SwitchRule(horizon=3))
        old = make_version("ENSEMBLE", {"RW": 0.5, "AR_BIC": 0.5}, code_source="old")
        new = make_version("ENSEMBLE", {"RW": 0.3, "AR_BIC": 0.7}, code_source="new")
        loop.register(old)
        journal.activate(old.version_id, "initial adoption")
        # observations feed the fast contour (state estimates only)
        for step in range(3):
            report = loop.observe(Observation(origin=f"2026-{step + 11:02d}", values={"state": 1.0},
                                              vintage_hash="d" * 64))
            self.assertTrue(report["admissible"])
        base = squared_error_series(seed=41)
        promoted = loop.consider_candidate(new, {loss: 0.49 * base for loss in ("RMSE", "CRPS")},
                                           {loss: base for loss in ("RMSE", "CRPS")},
                                           target="T1_CPI_YOY")
        self.assertEqual(promoted["action"], "PROMOTE")
        self.assertEqual(journal.active, new.version_id)
        rollback = loop.check_rollback(old, {loss: 0.49 * base for loss in ("RMSE", "CRPS")},
                                       {loss: base for loss in ("RMSE", "CRPS")},
                                       target="T1_CPI_YOY")
        self.assertEqual(rollback["action"], "ROLLBACK")
        self.assertEqual(journal.active, old.version_id)
        snapshot = loop.snapshot()
        self.assertEqual(snapshot["journal"]["active"], old.version_id)
        self.assertTrue(snapshot["journal"]["valid"])
        self.assertGreaterEqual(journal.history("SWITCH")[-1]["seq"], 3)

    def test_loop_records_drift_reports(self):
        rng = np.random.default_rng(51)
        monitor = DriftMonitor(reference_features={"x": rng.normal(0, 1, 200)},
                               reference_errors=rng.normal(0, 1, 200))
        loop = MonitoringLoop(drift_monitor=monitor)
        report = loop.observe(Observation(origin="2026-11", values={"state": 0.0},
                                          vintage_hash="e" * 64),
                              features={"x": rng.normal(4.0, 1.0, 200)},
                              errors=rng.normal(2.0, 1.0, 200))
        self.assertIsNotNone(report["drift"])
        self.assertTrue(report["drift"]["features"]["drift_detected"])
        self.assertTrue(report["drift"]["errors"]["drift_detected"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
