"""Tests for the SPF-augmented T2 candidate (``tools/spf_augmented_candidate.py``).

The suite is offline apart from the optional local REAL_TIME data (the git-ignored ALFRED vintage
archive and SPF values); the tests that need them are skipped when absent, exactly like the other
evaluation tests.  Nothing here is registered anywhere: the file is a *definition proposal* for the
Lead, so the tests assert the proposal is well-formed and that the forecast entry point is
harness-compatible.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import spf_augmented_candidate as cand  # noqa: E402
from amfe.ensemble.sources import load_spf_medians, spf_available  # noqa: E402
import evaluate  # noqa: E402

LOCAL_DATA = (ROOT / "data" / "raw").exists() and all(
    (ROOT / "data" / "raw" / name / "latest.csv").exists()
    for name in ("GDPC1", "CPIAUCSL", "CPILFESL", "UNRATE", "GS10", "TB3MS", "USREC"))
LOCAL_SPF = spf_available()
SPF_ORIGIN = "2019-05-31"     # 2019Q2 middle-month end; SPF news release is public by then (fallback
                              # anchor = last day of May, so the origin is exactly the release date)
FALLBACK_ORIGIN = "2019-04-30"  # not a middle month: the SPF member cannot enter


class Definitions(unittest.TestCase):
    def test_candidate_ids_order_and_roles(self):
        self.assertEqual(cand.CANDIDATE_IDS, (cand.CANDIDATE_ID_NOT_SPF, cand.CANDIDATE_ID_SPF))
        control = cand.candidate_definition(cand.CANDIDATE_ID_NOT_SPF)
        augmented = cand.candidate_definition(cand.CANDIDATE_ID_SPF)
        self.assertEqual(control["role"], "control")
        self.assertEqual(augmented["role"], "candidate")
        self.assertEqual(control["id"], "ENSEMBLE_T2_NOT_SPF")
        self.assertEqual(augmented["id"], "ENSEMBLE_T2_SPF")

    def test_definition_members_and_mapping(self):
        control = cand.candidate_definition(cand.CANDIDATE_ID_NOT_SPF)
        augmented = cand.candidate_definition(cand.CANDIDATE_ID_SPF)
        self.assertEqual(control["members"], cand.internal_members(0))
        self.assertEqual(len(control["members"]), 4)
        self.assertEqual(augmented["members"], cand.internal_members(0) + [cand.EXTERNAL_MEMBER])
        self.assertEqual(len(augmented["members"]), 5)
        self.assertEqual(augmented["spf_mapping"], {"0": "drgdp2", "1": "drgdp3",
                                                    "2": "drgdp4", "4": "drgdp6"})
        self.assertEqual(control["targets"], ["T2_GDP_GROWTH"])
        self.assertEqual(control["horizons_quarters"], [0, 1, 2, 4])

    def test_definition_code_hash_and_status(self):
        for candidate_id in cand.CANDIDATE_IDS:
            definition = cand.candidate_definition(candidate_id)
            code_file = ROOT / definition["code_file"]
            self.assertTrue(code_file.exists())
            self.assertRegex(definition["code_sha256"], r"^[0-9a-f]{64}$")
            expected = hashlib.sha256(code_file.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            self.assertEqual(definition["code_sha256"], expected)
            self.assertEqual(definition["registration_status"], "NOT_REGISTERED")
            self.assertIn("equal weights", definition["weight_scheme"])
            self.assertTrue(definition["availability_rule"])
            self.assertTrue(definition["fallback"])

    def test_candidates_document_is_a_proposal_only(self):
        document = cand.candidates_document()
        self.assertFalse(document["registered"])
        self.assertEqual(document["document_id"], "AMFE-CANDIDATES")
        self.assertIn("proposal", document["note"].lower())
        self.assertEqual([entry["id"] for entry in document["candidates"]],
                         list(cand.CANDIDATE_IDS))
        self.assertNotIn("registered_on", document,
                         "the tool must not pretend to register a candidate")

    def test_spec_and_limitations(self):
        spec = cand.spec()
        self.assertEqual(spec["target"], "T2_GDP_GROWTH")
        self.assertEqual(spec["registration_status"], "NOT_REGISTERED")
        self.assertEqual(spec["base_ensemble"], "ENSEMBLE_EQUAL")
        self.assertEqual(spec["external_member"], "SPF_GDP_MEDIAN")
        self.assertEqual(spec["candidate_ids"], [cand.CANDIDATE_ID_NOT_SPF, cand.CANDIDATE_ID_SPF])
        self.assertEqual(spec["registration_order"],
                         "control (NOT_SPF) first, SPF-augmented second")
        self.assertGreaterEqual(len(spec["limitations"]), 4)
        self.assertIn("T2_GDP_GROWTH", spec["target_definition"])


class MemberPlan(unittest.TestCase):
    def test_control_never_has_the_spf_member(self):
        for origin, horizon in ((SPF_ORIGIN, 0), (FALLBACK_ORIGIN, 0), ("1990-08-31", 0)):
            plan = cand.member_plan(cand.CANDIDATE_ID_NOT_SPF, "T2_GDP_GROWTH", origin, horizon)
            self.assertEqual(plan["member_ids"], cand.internal_members(horizon))
            self.assertFalse(plan["spf_available"])
            self.assertEqual(plan["spf_reason"], "NOT_APPLICABLE_NOT_SPF")
            self.assertFalse(plan["spf_fallback"])

    @unittest.skipUnless(LOCAL_SPF, "local git-ignored SPF values are absent")
    def test_spf_member_enters_by_the_release_calendar(self):
        plan = cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", SPF_ORIGIN, 0)
        self.assertTrue(plan["spf_available"])
        self.assertFalse(plan["spf_fallback"])
        self.assertEqual(plan["member_ids"], cand.internal_members(0) + [cand.EXTERNAL_MEMBER])
        self.assertEqual(plan["origin"], SPF_ORIGIN)
        # a middle-month origin before the official release of that survey is a fallback
        before_release = cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", "2019-02-28", 0)
        self.assertFalse(before_release["spf_available"])
        self.assertEqual(before_release["spf_reason"], "SPF_NEWS_RELEASE_NOT_PUBLISHED_AT_ORIGIN")
        self.assertTrue(before_release["spf_fallback"])

    def test_non_deadline_month_and_pre_real_time_fallbacks(self):
        non_middle = cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", FALLBACK_ORIGIN, 0)
        self.assertEqual(non_middle["spf_reason"], "ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH")
        self.assertTrue(non_middle["spf_fallback"])
        pre = cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", "1990-02-28", 0)
        self.assertEqual(pre["spf_reason"], "SPF_NOT_REAL_TIME_BEFORE_1990Q3")

    def test_quarter_end_horizons_fall_back(self):
        for horizon, origin in ((1, "2019-03-29"), (2, "2019-03-29"), (4, "2019-03-29")):
            plan = cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", origin, horizon)
            self.assertFalse(plan["spf_available"], horizon)
            self.assertEqual(plan["spf_reason"], "ORIGIN_IS_NOT_AN_SPF_DEADLINE_MONTH")
            self.assertTrue(plan["spf_fallback"])

    def test_missing_values_fall_back_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", SPF_ORIGIN, 0,
                                    data_root=Path(tmp))
        self.assertFalse(plan["spf_available"])
        self.assertEqual(plan["spf_reason"], "SPF_VALUES_NOT_DOWNLOADED")
        self.assertTrue(plan["spf_fallback"])
        self.assertEqual(plan["member_ids"], cand.internal_members(0))


class Guards(unittest.TestCase):
    def test_t2_only_guard(self):
        for target in ("T1_CPI_YOY", "T3_RECESSION", "UNKNOWN"):
            with self.assertRaises(cand.CandidateError):
                cand.member_plan(cand.CANDIDATE_ID_SPF, target, SPF_ORIGIN, 0)

    def test_horizon_guard(self):
        for horizon in (3, 5, -1):
            with self.assertRaises(cand.CandidateError):
                cand.member_plan(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", SPF_ORIGIN, horizon)

    def test_unknown_candidate_guard(self):
        with self.assertRaises(cand.CandidateError):
            cand.candidate_variant("ENSEMBLE_UNKNOWN")


@unittest.skipUnless(LOCAL_DATA, "local ALFRED data archive is absent (git-ignored data/raw)")
class ForecastFormat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = cand.candidate_forecast(cand.CANDIDATE_ID_NOT_SPF, "T2_GDP_GROWTH",
                                              FALLBACK_ORIGIN, 0)
        cls.augmented = cand.candidate_forecast(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH",
                                                SPF_ORIGIN, 0)

    def test_harness_compatible_dict(self):
        result = self.augmented
        for key in ("candidate_id", "target", "origin", "horizon", "point", "quantiles",
                    "probability", "data_class", "members", "member_weights",
                    "candidate_variant", "spf_available", "spf_reason", "spf_fallback",
                    "spf_horizon_code", "definition"):
            self.assertIn(key, result, key)
        self.assertIsInstance(result["point"], float)
        self.assertEqual(result["candidate_id"], cand.CANDIDATE_ID_SPF)
        self.assertEqual(result["candidate_variant"], "SPF")
        self.assertEqual(result["target"], "T2_GDP_GROWTH")
        self.assertIn(result["data_class"], {"REAL_TIME", "PSEUDO_REAL_TIME"})
        self.assertEqual(result["definition"]["registration_status"], "NOT_REGISTERED")
        quantiles = np.asarray(result["quantiles"], dtype=float)
        self.assertEqual(quantiles.size, 19)
        self.assertTrue(np.all(np.isfinite(quantiles)))
        self.assertTrue(np.all(np.diff(quantiles) >= -1e-9))
        weights = result["member_weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)

    def test_output_is_consumable_by_the_loss_functions(self):
        result = self.augmented
        actual = float(result["point"])  # any finite target works for the format check
        se = evaluate.rmse_squared_error([result["point"]], [actual])
        self.assertEqual(float(se[0]), 0.0)
        crps = evaluate.crps(np.asarray([result["quantiles"]]), np.asarray([actual]))
        self.assertTrue(np.isfinite(crps).all())

    def test_augmented_candidate_has_five_members_when_available(self):
        result = self.augmented
        self.assertTrue(result["spf_available"])
        self.assertFalse(result["spf_fallback"])
        self.assertEqual(len(result["member_weights"]), 5)
        self.assertIn(cand.EXTERNAL_MEMBER, result["member_weights"])
        self.assertAlmostEqual(result["member_weights"][cand.EXTERNAL_MEMBER], 0.2, places=12)
        self.assertEqual(result["members"], cand.internal_members(0) + [cand.EXTERNAL_MEMBER])

    def test_fallback_forecast_is_identical_to_the_control(self):
        result = cand.candidate_forecast(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", FALLBACK_ORIGIN, 0)
        self.assertFalse(result["spf_available"])
        self.assertTrue(result["spf_fallback"])
        self.assertEqual(result["member_weights"].keys(), self.control["member_weights"].keys())
        self.assertAlmostEqual(result["point"], self.control["point"], places=12)
        np.testing.assert_allclose(result["quantiles"], self.control["quantiles"], atol=1e-12)

    def test_no_spf_value_is_exposed_in_the_output(self):
        text = json.dumps(self.augmented)
        self.assertNotIn('"value"', text)
        for item in self.augmented.get("external_forecasts") or []:
            self.assertNotIn("value", item)
        self.assertIn("research use", json.dumps(self.definition_code_hashes()))
        self.assertIn("Philadelphia", json.dumps(cand.spec()["spf_attribution"]))

    def definition_code_hashes(self):
        return [cand.candidate_definition(candidate_id) for candidate_id in cand.CANDIDATE_IDS]

    @unittest.skipUnless(LOCAL_SPF, "local git-ignored SPF values are absent")
    def test_forecast_is_deterministic(self):
        first = cand.candidate_forecast(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", SPF_ORIGIN, 0)
        second = cand.candidate_forecast(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH", SPF_ORIGIN, 0)
        self.assertEqual(first["point"], second["point"])
        np.testing.assert_allclose(first["quantiles"], second["quantiles"], atol=0.0)
        self.assertEqual(first["member_weights"], second["member_weights"])

    def test_horizons_run_and_fall_back_at_quarter_ends(self):
        for horizon in (1, 2, 4):
            result = cand.candidate_forecast(cand.CANDIDATE_ID_SPF, "T2_GDP_GROWTH",
                                             "2019-03-29", horizon)
            self.assertEqual(result["horizon"], horizon)
            self.assertEqual(result["spf_horizon_code"],
                             cand.SPF_CODE_BY_HORIZON[horizon])
            self.assertTrue(result["spf_fallback"], horizon)
            self.assertIsInstance(result["point"], float)


class Cli(unittest.TestCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cand.main(argv)
        return code, buffer.getvalue()

    def test_definition_cli(self):
        code, text = self._run(["--definition"])
        self.assertEqual(code, 0)
        document = json.loads(text)
        self.assertFalse(document["registered"])
        self.assertEqual(len(document["candidates"]), 2)

    def test_spec_cli(self):
        code, text = self._run(["--spec"])
        self.assertEqual(code, 0)
        spec = json.loads(text)
        self.assertEqual(spec["tool"], cand.TOOL)
        self.assertEqual(spec["horizons_quarters"], [0, 1, 2, 4])

    def test_check_cli(self):
        code, text = self._run(["--check", "--window", "W1"])
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertIn("spec", payload)
        availability = payload["availability"]
        self.assertEqual(availability["target"], "T2_GDP_GROWTH")
        self.assertIn("0", availability["horizons"])
        for horizon in ("1", "2", "4"):
            # the frozen alignment rule cannot admit SPF at quarter-end origins
            self.assertEqual(availability["horizons"][horizon]["spf_available"], 0, horizon)
        for block in availability["horizons"].values():
            self.assertGreater(block["origins"], 0)
            if block["spf_fraction"] is not None:
                self.assertGreaterEqual(block["spf_fraction"], 0.0)
                self.assertLessEqual(block["spf_fraction"], 1.0)

    def test_no_command_requires_an_action(self):
        with self.assertRaises(SystemExit):
            cand.main([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
