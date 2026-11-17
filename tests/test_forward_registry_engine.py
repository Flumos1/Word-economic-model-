#!/usr/bin/env python3
"""Tests of the forward-registry engine (wave 5, track B).

Covered: the protocol record schema, append-only identity rules in the journal, the backdating
and future-origin refusals, determinism of the generated rows, resolution on a synthetic
journal, the C6 counter through ``tools/evaluate.c6_forward_check`` (n < 12 fails) and the
absence of raw actual values in the resolve/status output.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import forward_forecast  # noqa: E402
import registry  # noqa: E402
import registry_resolve  # noqa: E402

COMMIT = "0f9eb3579975a4f1e93c10b4df69dd6ab9836cc9"
VINTAGE = "a" * 64
SCORING = "b" * 64
REGISTERED = "2026-11-12T00:00:00Z"
QUANTILES = [round(1.0 + 0.1 * i, 2) for i in range(1, 20)]
RAW_CPIAUCSL = ROOT / "data" / "raw" / "CPIAUCSL"


def make_row(candidate_id="ENSEMBLE_ADAPTIVE", target="T1_CPI_YOY", horizon=1,
             origin="2026-11", point=2.0, quantiles=None, probability=None,
             control_arm=False) -> dict:
    return forward_forecast.build_record(
        candidate_id, target, horizon, origin, commit=COMMIT, vintage=VINTAGE,
        scoring=SCORING, point=point, quantiles=QUANTILES if quantiles is None else quantiles,
        event_probability=probability, registered_at=REGISTERED, control_arm=control_arm,
        notes="test row")


class RecordSchemaTest(unittest.TestCase):
    def test_quantile_record_is_valid(self):
        self.assertEqual(registry.validate_record(make_row()), [])

    def test_record_carries_every_protocol_field(self):
        row = make_row()
        for field in ("candidate_id", "code_commit", "data_vintage_hash", "target", "horizon",
                      "origin", "quantiles_5_to_95_step_5", "point", "scoring_rule_commit"):
            self.assertIn(field, row)
        self.assertEqual(len(row["quantiles_5_to_95_step_5"]), 19)

    def test_event_record_has_probability_and_no_quantiles(self):
        row = make_row(target="T3_RECESSION", horizon=3, probability=0.42)
        self.assertEqual(registry.validate_record(row), [])
        self.assertIsNone(row["point"])
        self.assertNotIn("quantiles_5_to_95_step_5", row)
        self.assertEqual(row["event_probability"], 0.42)

    def test_non_monotone_quantiles_are_rejected(self):
        bad = list(QUANTILES)
        bad[5], bad[6] = bad[6], bad[5]
        problems = registry.validate_record(make_row(quantiles=bad))
        self.assertTrue(any("increasing" in problem for problem in problems))

    def test_unknown_field_is_rejected(self):
        row = make_row()
        row["raw_cpi_value"] = 312.5
        self.assertTrue(any("unknown field" in p for p in registry.validate_record(row)))


class AppendOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "forecasts.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_duplicate_identity_is_refused(self):
        result = registry.append_record(make_row(), self.journal)
        self.assertTrue(result["appended"])
        with self.assertRaises(registry.RegistryError):
            registry.append_record(make_row(), self.journal)
        self.assertEqual(len(registry.read_rows(self.journal)), 1)

    def test_correction_references_the_replaced_row(self):
        registry.append_record(make_row(), self.journal)
        first = registry.read_rows(self.journal)[0]
        corrected = make_row(point=2.5)
        corrected["correction_of"] = registry.record_digest(first)
        registry.append_record(corrected, self.journal)
        rows, problems = registry.validate_journal(self.journal)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 2)

    def test_different_identity_is_a_new_row(self):
        registry.append_record(make_row(), self.journal)
        registry.append_record(make_row(candidate_id="ENSEMBLE_INVERSE_MSE"), self.journal)
        self.assertEqual(len(registry.read_rows(self.journal)), 2)


class OriginGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "forecasts.jsonl"
        registry.append_record(make_row(origin="2026-11"), self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def test_past_origin_is_refused(self):
        with self.assertRaises(forward_forecast.ForwardEngineError):
            forward_forecast.check_origin("2026-10", self.journal)

    def test_future_origin_is_refused(self):
        current = date.today()
        future = f"{current.year + 1:04d}-01"
        with self.assertRaises(forward_forecast.ForwardEngineError):
            forward_forecast.check_origin(future, self.journal)

    def test_current_month_origin_is_a_pre_origin_row(self):
        current = date.today()
        result = forward_forecast.check_origin(f"{current.year:04d}-{current.month:02d}",
                                               self.journal)
        self.assertEqual(result["origin"], result["current_month"])
        self.assertTrue(result["pre_origin"])

    @unittest.skipUnless(RAW_CPIAUCSL.exists(),
                         "local raw ALFRED archive is absent (git-ignored data/raw)")
    def test_published_fact_is_refused(self):
        # October 2026 CPI is published on 2026-11-12, so the October origin cannot be recorded
        published = forward_forecast.fact_available("T1_CPI_YOY", "2026-10", 1)
        self.assertTrue(published["published"])
        # the November-origin target (November CPI) is not published yet
        not_published = forward_forecast.fact_available("T1_CPI_YOY", "2026-11", 1)
        self.assertFalse(not_published["published"])


class DeterminismTest(unittest.TestCase):
    def test_record_bytes_are_deterministic(self):
        first = registry.canonical_json(make_row())
        second = registry.canonical_json(make_row())
        self.assertEqual(first, second)
        self.assertEqual(registry.record_digest(make_row()), registry.record_digest(make_row()))

    @unittest.skipUnless(RAW_CPIAUCSL.exists(),
                         "local raw ALFRED archive is absent (git-ignored data/raw)")
    def test_vintage_hash_repeats_and_has_no_raw_values(self):
        value_a = forward_forecast.data_vintage_hash("2026-11", "latest")
        value_b = forward_forecast.data_vintage_hash("2026-11", "latest")
        self.assertEqual(value_a, value_b)
        self.assertRegex(value_a, r"^[0-9a-f]{64}$")


def _past_origins(n_rows: int) -> list[str]:
    """The last ``n_rows`` month-end origins before the current month (no future origins)."""
    today = date.today()
    total = today.year * 12 + (today.month - 1)
    origins = []
    for step in range(1, n_rows + 1):
        index = total - step
        origins.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
    return list(reversed(origins))


def synthetic_journal(path: Path, n_rows: int, *, candidate="ENSEMBLE_ADAPTIVE") -> None:
    """Synthetic journal: candidate rows plus the two control arms on the same origins."""
    for i, origin in enumerate(_past_origins(n_rows)):
        point = 2.0 + 0.1 * i
        quantiles = [round(value + 0.1 * i, 2) for value in QUANTILES]
        registry.append_record(make_row(candidate, "T1_CPI_YOY", 1, origin, point=point,
                                        quantiles=quantiles), path)
        registry.append_record(make_row("EQUAL_WEIGHT_POINT", "T1_CPI_YOY", 1, origin,
                                        point=point + 0.2, control_arm=True), path)
        registry.append_record(make_row("EQUAL_WEIGHT_DENSITY", "T1_CPI_YOY", 1, origin,
                                        point=point + 0.2,
                                        quantiles=[round(v + 0.2, 2) for v in quantiles],
                                        control_arm=True), path)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "forecasts.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def actual_for(target, origin, horizon):
        return 2.05 + 0.1 * int(str(origin)[5:7])
    def test_resolution_uses_the_recorded_control_arms(self):
        synthetic_journal(self.journal, 3)
        report = registry_resolve.resolve_report(self.journal, actual_for=self.actual_for)
        self.assertEqual(report["resolved_rows"], 9)
        resolution = report["resolutions"][0]
        self.assertIn("cumulative_relative_loss", resolution)
        self.assertGreater(resolution["cumulative_relative_loss"], 0.0)
        self.assertIn("coverage", resolution)
        self.assertNotIn("actual", resolution)  # raw actuals never enter the report

    def test_unresolved_rows_do_not_enter_the_count(self):
        synthetic_journal(self.journal, 3)
        report = registry_resolve.status_report(
            self.journal, actual_for=lambda target, origin, horizon: None)
        self.assertEqual(report["resolved_rows"], 0)
        self.assertFalse(report["pass"])
        self.assertEqual(report["n_resolved"], 0)

    def test_c6_short_block_fails(self):
        synthetic_journal(self.journal, 3)
        report = registry_resolve.status_report(self.journal, actual_for=self.actual_for)
        checks = report["c6_checks"]
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual(check["n_resolved"], 3)
        self.assertEqual(check["min_forecasts"], 12)
        self.assertFalse(check["count_met"])
        self.assertFalse(check["pass"])

    def test_c6_full_block_with_better_forecast_passes(self):
        import evaluate

        candidate_quantiles = [[2.0 + 2.0 * i / 18 for i in range(19)]] * 12
        comparator_quantiles = [[1.0 + 4.0 * i / 18 for i in range(19)]] * 12
        better = evaluate.c6_forward_check(
            "T1_CPI_YOY", [3.0] * 12,
            candidate_point=[3.0] * 12,
            candidate_quantiles=candidate_quantiles,
            comparator_point=[3.5] * 12,
            comparator_quantiles=comparator_quantiles)
        self.assertTrue(better["count_met"])
        self.assertTrue(better["per_loss"]["RMSE"]["relative_loss_met"])
        self.assertTrue(better["per_loss"]["CRPS"]["relative_loss_met"])
        self.assertEqual(set(better["coverage"]), {"nominal_0.50", "nominal_0.80", "nominal_0.90"})

    def test_c6_short_block_is_a_hard_fail(self):
        import evaluate

        short = evaluate.c6_forward_check(
            "T1_CPI_YOY", [3.0] * 11,
            candidate_point=[3.0] * 11,
            candidate_quantiles=[[2.0 + 2.0 * i / 18 for i in range(19)]] * 11,
            comparator_point=[3.5] * 11,
            comparator_quantiles=[[1.0 + 4.0 * i / 18 for i in range(19)]] * 11)
        self.assertEqual(short["n_resolved"], 11)
        self.assertFalse(short["count_met"])
        self.assertFalse(short["pass"])

    def test_status_output_has_no_raw_forecast_or_actual_arrays(self):
        synthetic_journal(self.journal, 3)
        report = registry_resolve.status_report(self.journal, actual_for=self.actual_for)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("quantiles_5_to_95_step_5", text)
        self.assertNotIn("primary_vintage", text)
        self.assertNotIn('"actual"', text)


class WorkflowTest(unittest.TestCase):
    path = ROOT / ".github" / "workflows" / "forward-registry.yml"

    def test_workflow_is_valid_yaml_and_has_no_commit_step(self):
        import yaml

        document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.assertIn("schedule", document[True])
        self.assertIn("workflow_dispatch", document[True])
        text = self.path.read_text(encoding="utf-8").lower()
        self.assertIn("tools/forward_forecast.py", text)
        self.assertIn("tools/registry.py --validate", text)
        self.assertIn("tools/registry_resolve.py", text)
        for forbidden in ("git push", "git commit", "gh pr create"):
            self.assertNotIn(forbidden, text)
        steps = document["jobs"]["forward-registry"]["steps"]
        self.assertTrue(any("upload-artifact" in json.dumps(step) for step in steps))


if __name__ == "__main__":
    unittest.main(verbosity=2)
