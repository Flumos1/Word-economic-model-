"""Tests for the learning/validation loop (docs/151): Q, model registry, walk-forward, demo."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.learning import quality as q  # noqa: E402
from amfe.learning import registry as reg  # noqa: E402
from amfe.learning import walkforward as wf  # noqa: E402
import learning_demo  # noqa: E402


# --------------------------------------------------------------------------------------- fixtures
def metrics(**overrides) -> dict:
    base = {"error": 0.5, "benchmark_error": 1.0, "balance_residual_max": 0.0,
            "n_oos": 60, "stability_cv": 0.4, "interval_coverage": 0.8,
            "consistency_violations": 0}
    base.update(overrides)
    return base


def quality_document(**overrides) -> dict:
    return q.evaluate_quality(metrics(**overrides)).as_dict()


def ar1_forecast(values, horizon: int) -> dict:
    y = [float(value) for value in values]
    n = len(y)
    mean_x = sum(y[:-1]) / (n - 1)
    mean_y = sum(y[1:]) / (n - 1)
    cov = sum((y[i] - mean_x) * (y[i + 1] - mean_y) for i in range(n - 1))
    var = sum((y[i] - mean_x) ** 2 for i in range(n - 1))
    phi = cov / var if var else 0.0
    intercept = mean_y - phi * mean_x
    path = []
    last = y[-1]
    for _ in range(horizon):
        last = intercept + phi * last
        path.append(last)
    return {"point": path,
            "quantiles": [{0.1: value - 1.0, 0.9: value + 1.0} for value in path]}


def random_walk(values, horizon: int) -> dict:
    return {"point": [float(values[-1])] * horizon}


class QualityFunction(unittest.TestCase):
    def test_components_are_normalized_and_weighted(self):
        result = q.evaluate_quality(metrics())
        self.assertGreaterEqual(result.q, 0.0)
        self.assertLessEqual(result.q, 1.0)
        for value in result.components.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=12)
        self.assertAlmostEqual(result.q,
                               sum(result.components[k] * result.weights[k]
                                   for k in result.components), places=12)
        self.assertEqual(result.grade, q.grade_for(result.q))

    def test_better_error_means_higher_q(self):
        better = q.evaluate_quality(metrics(error=0.5))
        worse = q.evaluate_quality(metrics(error=1.5))
        self.assertGreater(better.q, worse.q)
        self.assertGreater(better.components["E"], worse.components["E"])

    def test_optional_components_renormalize_weights(self):
        sparse = q.evaluate_quality({"error": 0.5, "benchmark_error": 1.0,
                                     "balance_residual_max": 0.0, "n_oos": 20})
        self.assertEqual(set(sparse.components), {"E", "C"})
        self.assertAlmostEqual(sum(sparse.weights.values()), 1.0, places=12)
        # coverage far from nominal lowers the risk component
        good = q.evaluate_quality(metrics(interval_coverage=0.8))
        bad = q.evaluate_quality(metrics(interval_coverage=0.2))
        self.assertGreater(good.components["R"], bad.components["R"])

    def test_grade_thresholds(self):
        self.assertEqual(q.grade_for(0.80), "A")
        self.assertEqual(q.grade_for(0.65), "B")
        self.assertEqual(q.grade_for(0.50), "C")
        self.assertEqual(q.grade_for(0.10), "D")

    def test_hard_constraints_fail(self):
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(balance_residual_max=1e-3))
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(future_leakage=True))
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(n_oos=3))
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(benchmark_error=0.0))
        with self.assertRaises(q.QualityError):
            q.evaluate_quality({"error": 0.5, "benchmark_error": 1.0})
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(error=float("nan")))

    def test_explicit_weights_must_match_components(self):
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(), weights={"E": 0.5, "C": 0.5})
        with self.assertRaises(q.QualityError):
            q.evaluate_quality(metrics(), weights={"E": 0.5, "C": 0.2, "S": 0.2, "R": 0.2})
        result = q.evaluate_quality(metrics(), weights={"E": 0.4, "C": 0.2, "S": 0.2, "R": 0.2})
        self.assertGreater(result.q, 0.0)

    def test_compare_versions(self):
        previous = quality_document(error=0.8)
        improved = quality_document(error=0.4)
        degraded = quality_document(error=1.5)
        self.assertEqual(q.compare_versions(previous, improved)["decision"], "IMPROVED")
        self.assertEqual(q.compare_versions(previous, degraded)["decision"], "DEGRADED")
        self.assertEqual(q.compare_versions(previous, previous)["decision"], "TIE")
        broken = dict(previous)
        broken["hard_constraints"] = {"pass": False}
        with self.assertRaises(q.QualityError):
            q.compare_versions(broken, improved)


class WalkForward(unittest.TestCase):
    def test_folds_never_use_future_data(self):
        folds = wf.folds(30, min_train=10, horizon=2, step=1)
        self.assertGreater(len(folds), 0)
        for fold in folds:
            self.assertEqual(fold["train_end_exclusive"], fold["origin_index"] + 1)
            self.assertEqual(fold["test_start"], fold["origin_index"] + 1)
            self.assertGreater(fold["test_start"], fold["train_end_exclusive"] - 1)
        wf.assert_no_leakage(folds)
        leaked = [dict(folds[0], test_start=1, test_end_exclusive=3)]
        with self.assertRaises(wf.WalkForwardError):
            wf.assert_no_leakage(leaked)

    def test_metrics_on_a_deterministic_ramp(self):
        series = [float(index) for index in range(25)]
        result = wf.run_walk_forward(series, ar1_forecast, min_train=10, horizon=1,
                                     include_raw=True)
        # an exact linear ramp is reproduced by AR(1); the naive benchmark is off by one step
        self.assertLess(result["model"]["mae"], 1e-6)
        self.assertAlmostEqual(result["benchmark"]["mae"], 1.0, places=9)
        self.assertTrue(result["comparison"]["beats_naive"])
        self.assertGreater(result["model"]["n_forecasts"], 10)
        self.assertTrue(result["leakage_checks"]["no_future"])
        for record in result["records"]:
            self.assertLess(record["train_size"], record["test_end_exclusive"])

    def test_mae_rmse_and_crps_known_values(self):
        self.assertAlmostEqual(wf.mae([1.0, -2.0, 3.0]), 2.0, places=12)
        self.assertAlmostEqual(wf.rmse([1.0, -2.0, 3.0]), math.sqrt(14.0 / 3.0), places=12)
        # one central quantile at 0.5 gives CRPS = |actual - median|
        self.assertAlmostEqual(wf.crps_from_quantiles({0.5: 10.0}, 12.0), 2.0, places=12)
        # a perfect quantile forecast has zero CRPS
        self.assertAlmostEqual(wf.crps_from_quantiles({0.1: 5.0, 0.9: 5.0}, 5.0), 0.0,
                               places=12)

    def test_naive_benchmark_comparison_and_custom_benchmark(self):
        series = [float(index % 5) for index in range(40)]
        result = wf.run_walk_forward(series, random_walk, min_train=12, horizon=1)
        self.assertAlmostEqual(result["comparison"]["relative_mae"], 1.0, places=9)
        self.assertFalse(result["comparison"]["beats_naive"])
        self.assertEqual(result["comparison"]["benchmark"], "naive_last")
        worse = wf.run_walk_forward(series, lambda values, horizon: {"point":
                                   [float(values[-1]) + 3.0] * horizon},
                                   min_train=12, horizon=1)
        self.assertGreater(worse["comparison"]["relative_mae"], 1.0)
        custom = wf.run_walk_forward(
            series, random_walk, min_train=12, horizon=1,
            benchmark=wf.naive_last_forecast)
        self.assertEqual(custom["comparison"]["benchmark"], "custom")

    def test_fail_closed_cases(self):
        with self.assertRaises(wf.WalkForwardError):
            wf.folds(10, min_train=12, horizon=1)
        with self.assertRaises(wf.WalkForwardError):
            wf.folds(30, min_train=0, horizon=1)
        with self.assertRaises(wf.WalkForwardError):
            wf.run_walk_forward([1.0] * 20, lambda values, horizon: {"point": [1.0] * 2},
                                min_train=10, horizon=1)
        with self.assertRaises(wf.WalkForwardError):
            wf.run_walk_forward([1.0] * 20, lambda values, horizon: {"point": [float("nan")]},
                                min_train=10, horizon=1)
        with self.assertRaises(wf.WalkForwardError):
            wf.run_walk_forward([1.0] * 20, lambda values, horizon: {"point": [1.0]},
                                min_train=10, horizon=0)
        with self.assertRaises(wf.WalkForwardError):
            wf.run_walk_forward([0.0] * 20, random_walk, min_train=10, horizon=1)

    def test_determinism(self):
        series = [math.sin(index / 3.0) * 10.0 + 20.0 for index in range(40)]
        first = wf.run_walk_forward(series, ar1_forecast, min_train=12, horizon=2)
        second = wf.run_walk_forward(series, ar1_forecast, min_train=12, horizon=2)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertIsNotNone(first["quality_inputs"]["interval_coverage"])


class Registry(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "registry.jsonl"
        self.criteria = {"registered_at": "2026-11-01",
                         "criteria": {"min_q": 0.6, "min_relative_mae": 0.95}}
        self.review = {"reviewer": "independent-reviewer", "independent": True, "pass": True,
                       "reviewed_at": "2026-11-16",
                       "report_sha256": reg.sha256_text("review-report")}

    _DEFAULT = object()

    def _register(self, version="v1", criteria=_DEFAULT, quality=None, **kwargs):
        selected = self.criteria if criteria is self._DEFAULT else criteria
        return reg.register(self.path, model_id="M", version=version,
                            code_sha256=reg.sha256_text("code"), data_sha256=reg.sha256_text("data"),
                            metrics={"mae": 0.5}, quality=quality or quality_document(),
                            evaluated_at="2026-11-15", criteria=selected, **kwargs)

    def _to_limited(self, version="v1"):
        reg.promote(self.path, model_id="M", version=version, to_status="SHADOW", at="2026-11-16")
        reg.promote(self.path, model_id="M", version=version, to_status="LIMITED", at="2026-11-16")

    def test_append_only_log(self):
        self._register()
        first_bytes = self.path.read_bytes()
        self._register(version="v2")
        second_bytes = self.path.read_bytes()
        self.assertTrue(second_bytes.startswith(first_bytes))
        lines = [line for line in second_bytes.decode("utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            event = json.loads(line)
            self.assertEqual(event["schema"], reg.SCHEMA)
            self.assertEqual(line, reg.canonical_event(event))

    def test_illegal_transitions_and_duplicates(self):
        self._register()
        with self.assertRaises(reg.RegistryError):
            self._register()
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v1", to_status="LIMITED",
                        at="2026-11-16")
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v1", to_status="SHADOW", at="2026-11-16")
            reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                        at="2026-11-16")

    def test_active_requires_preregistered_criteria(self):
        self._register(criteria=None)
        self._to_limited()
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                        at="2026-11-16", independent_review=self.review)
        # criteria registered after the evaluation are not "preregistered"
        with self.assertRaises(reg.RegistryError):
            reg.register(self.path, model_id="M", version="late",
                         code_sha256=reg.sha256_text("code"),
                         data_sha256=reg.sha256_text("data"), metrics={"mae": 0.5},
                         quality=quality_document(), evaluated_at="2026-11-15",
                         criteria={"registered_at": "2026-11-16",
                                   "criteria": {"min_q": 0.6}})

    def test_active_requires_independent_review(self):
        self._register()
        self._to_limited()
        with self.assertRaises(reg.RegistryError) as context:
            reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                        at="2026-11-16")
        self.assertIn("independent review", str(context.exception))
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                        at="2026-11-16",
                        independent_review=dict(self.review, independent=False))
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                        at="2026-11-16",
                        independent_review=dict(self.review, reviewer="automation"))
        failed_review = dict(self.review)
        failed_review["pass"] = False
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                        at="2026-11-16", independent_review=failed_review)

    def test_active_promotion_succeeds_with_both_prerequisites(self):
        self._register()
        self._to_limited()
        reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                    at="2026-11-16", independent_review=self.review)
        view = reg.model_status(self.path, "M")
        self.assertEqual(view["active_version"], "v1")
        self.assertEqual(view["versions"]["v1"], "ACTIVE")
        report = reg.validate_registry(self.path)
        self.assertTrue(report["valid"], report["errors"])
        # a second ACTIVE version is refused until a revert
        self._register(version="v2")
        reg.promote(self.path, model_id="M", version="v2", to_status="SHADOW", at="2026-11-16")
        reg.promote(self.path, model_id="M", version="v2", to_status="LIMITED", at="2026-11-16")
        with self.assertRaises(reg.RegistryError):
            reg.promote(self.path, model_id="M", version="v2", to_status="ACTIVE",
                        at="2026-11-16",
                        independent_review=dict(self.review, report_sha256=reg.sha256_text("r2")))

    def test_reject_and_revert(self):
        self._register()
        self._to_limited()
        reg.promote(self.path, model_id="M", version="v1", to_status="ACTIVE",
                    at="2026-11-16", independent_review=self.review)
        self._register(version="v2")
        reg.promote(self.path, model_id="M", version="v2", to_status="SHADOW", at="2026-11-16")
        reg.reject(self.path, model_id="M", version="v2", at="2026-11-16", reason="degraded")
        view = reg.model_status(self.path, "M")
        self.assertEqual(view["versions"]["v2"], "REJECTED")
        # ACTIVE cannot be rejected directly; only reverted (tested separately)
        with self.assertRaises(reg.RegistryError):
            reg.reject(self.path, model_id="M", version="v1", at="2026-11-16",
                       reason="direct rejection of ACTIVE")
        self.assertTrue(reg.validate_registry(self.path)["valid"])

    def test_rollback_to_previous_active_version(self):
        self._register(version="stable")
        self._to_limited("stable")
        reg.promote(self.path, model_id="M", version="stable", to_status="ACTIVE",
                    at="2026-11-16", independent_review=self.review)
        self._register(version="candidate")
        self._to_limited("candidate")
        reg.revert(self.path, model_id="M", from_version="candidate", to_version="stable",
                   at="2026-11-16", reason="candidate cannot pass the human review")
        view = reg.model_status(self.path, "M")
        self.assertEqual(view["active_version"], "stable")
        self.assertEqual(view["versions"]["candidate"], "REJECTED")
        self.assertEqual(view["versions"]["stable"], "ACTIVE")
        self.assertTrue(reg.validate_registry(self.path)["valid"])

    def test_validate_detects_corruption(self):
        self._register()
        path = Path(self.directory.name) / "broken.jsonl"
        path.write_text("{oops\n", encoding="utf-8")
        report = reg.validate_registry(path)
        self.assertFalse(report["valid"])
        self.assertIn("not JSON", report["errors"][0])
        with self.assertRaises(reg.RegistryError):
            reg.load_events(path)

    def test_registry_determinism(self):
        other = Path(self.directory.name) / "second.jsonl"
        for target in (self.path, other):
            reg.register(target, model_id="M", version="v1",
                         code_sha256=reg.sha256_text("code"),
                         data_sha256=reg.sha256_text("data"), metrics={"mae": 0.5},
                         quality=quality_document(), evaluated_at="2026-11-15",
                         criteria=self.criteria)
            reg.promote(target, model_id="M", version="v1", to_status="SHADOW", at="2026-11-16")
        self.assertEqual(self.path.read_bytes(), other.read_bytes())


class Demo(unittest.TestCase):
    def test_demo_shows_improvement_degradation_and_rollback(self):
        report = learning_demo.run_demo()
        versions = report["versions"]
        self.assertEqual(set(versions), {"v0", "v1", "v2"})
        self.assertGreater(versions["v1"]["q"], versions["v0"]["q"])
        self.assertLess(versions["v2"]["q"], versions["v0"]["q"])
        self.assertTrue(versions["v1"]["metrics"]["beats_naive"])
        self.assertFalse(versions["v2"]["metrics"]["beats_naive"])
        self.assertEqual(report["decisions"]["v1_vs_v0"]["decision"], "IMPROVED")
        self.assertEqual(report["decisions"]["v2_vs_v0"]["decision"], "DEGRADED")
        self.assertEqual(len(report["refusals"]), 2)
        self.assertTrue(all(item["refused"] for item in report["refusals"]))
        # the two ATTEMPTS must exercise the review gate itself: v0 is still LIMITED when v1 is
        # tried, so a refusal cannot be masked by "already has an ACTIVE version"
        attempts = {item["attempt"] for item in report["refusals"]}
        self.assertEqual(attempts, {"no independent review", "non-independent review"})
        for item in report["refusals"]:
            reason = str(item["error"]).lower()
            self.assertIn("review", reason, item)
            self.assertNotIn("already has an active", reason, item)
        state_data = report["registry_state"]["DEMO_MODEL"]
        self.assertEqual(state_data["active_version"], "v0_rw_baseline")
        statuses = report["registry_validation"]["models"]["DEMO_MODEL"]["versions"]
        self.assertEqual(statuses["v2_ar1_biased_degraded"], "REJECTED")
        self.assertEqual(statuses["v1_ar1_improved"], "REJECTED")
        self.assertEqual(statuses["v0_rw_baseline"], "ACTIVE")
        self.assertTrue(report["registry_validation"]["valid"])
        self.assertTrue(report["warnings"])

    def test_demo_refusals_are_review_gate(self):
        """The v1->ACTIVE attempts must fail because of the review gate, not an existing ACTIVE."""
        report = learning_demo.run_demo()
        self.assertEqual(len(report["refusals"]), 2)
        for item in report["refusals"]:
            self.assertTrue(item["refused"])
            reason = str(item["error"]).lower()
            self.assertIn("review", reason, item)
            self.assertNotIn("already has an active", reason, item)
        # the review gate is checked while no version is ACTIVE yet
        events = learning_demo.load_events  # imported helper is not exposed; state is in report
        statuses = report["registry_validation"]["models"]["DEMO_MODEL"]["versions"]
        self.assertEqual(statuses["v0_rw_baseline"], "ACTIVE")
        self.assertEqual(statuses["v1_ar1_improved"], "REJECTED")

    def test_demo_is_deterministic(self):
        first = learning_demo.run_demo()
        second = learning_demo.run_demo()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_demo_writes_json_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "demo.json"
            code = learning_demo.main(["--out", str(out), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "AMFE-LEARNING-DEMO/1.0.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
