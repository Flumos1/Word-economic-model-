"""Tests for the research-only institutions block (docs/177 block 15, docs/203).

The block has no data and no network: everything here runs on the declared assumptions of
``amfe.world.institutions``.  The tests cover the composite score and its bounds, the weight
normalisation, the monotonicity of the four scenarios (score, premium, investment), the premium
floor/cap, the ramp path, the exported scenario modifier, determinism, the absence of raw data and
the fail-closed CLI behaviour.
"""
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

import institutions_scenarios as cli  # noqa: E402
from amfe.world import institutions as ins  # noqa: E402

_RUN = {}


def fixture():
    if not _RUN:
        _RUN.update(ins.run_all())
    return _RUN


def by_id(result):
    return {report["scenario"]["scenario_id"]: report for report in result["reports"]}


def mid_profile():
    return ins.find_example_profile("EXAMPLE-MID")


def overlay(scenario_id, profile=None, **parameters):
    scenario = next(item for item in ins.default_scenarios()
                    if item.scenario_id == scenario_id)
    return ins.scenario_overlay(profile or mid_profile(), scenario,
                                ins.OverlayParameters(**parameters) if parameters else None)


class ScoreTests(unittest.TestCase):
    def test_equal_weights_score_is_the_plain_mean(self):
        indicators = ins.InstitutionalIndicators(10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
        self.assertAlmostEqual(ins.institutional_score(indicators), 35.0)
        self.assertAlmostEqual(mid_profile().score(),
                               sum(mid_profile().indicators.as_tuple()) / 6.0)

    def test_score_bounds_are_respected(self):
        low = ins.InstitutionalIndicators(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        high = ins.InstitutionalIndicators(100.0, 100.0, 100.0, 100.0, 100.0, 100.0)
        self.assertEqual(ins.institutional_score(low), 0.0)
        self.assertEqual(ins.institutional_score(high), 100.0)
        for profile in ins.example_profiles():
            self.assertGreaterEqual(profile.score(), 0.0)
            self.assertLessEqual(profile.score(), 100.0)

    def test_custom_weights_change_the_score_and_are_normalised(self):
        indicators = ins.InstitutionalIndicators(10.0, 90.0, 50.0, 50.0, 50.0, 50.0)
        voice_only = {key: 0.0 for key in ins.INDICATOR_KEYS}
        voice_only["voice"] = 1.0
        self.assertAlmostEqual(ins.institutional_score(indicators, voice_only), 10.0)
        heavy = {key: 1.0 for key in ins.INDICATOR_KEYS}
        heavy["stability"] = 3.0  # relative weights, not shares
        normalised = ins.normalise_weights(heavy)
        self.assertAlmostEqual(sum(normalised.values()), 1.0)
        self.assertGreater(ins.institutional_score(indicators, heavy),
                           ins.institutional_score(indicators))
        self.assertEqual(ins.normalise_weights(None), ins.DEFAULT_WEIGHTS)

    def test_breakdown_contributions_sum_to_the_score(self):
        breakdown = ins.institutional_score_breakdown(mid_profile().indicators)
        self.assertAlmostEqual(sum(item["contribution"] for item in breakdown["contributions"]),
                               breakdown["score"], places=12)
        self.assertEqual([item["indicator"] for item in breakdown["contributions"]],
                         list(ins.INDICATOR_KEYS))
        self.assertEqual(set(breakdown["dimensions"]), set(ins.DIMENSIONS))
        self.assertAlmostEqual(breakdown["weight_sum"], 1.0)

    def test_dispersion_is_zero_for_a_flat_profile_and_grows_with_spread(self):
        flat = ins.InstitutionalIndicators(50.0, 50.0, 50.0, 50.0, 50.0, 50.0)
        self.assertAlmostEqual(flat.dispersion(), 0.0)
        spread = ins.InstitutionalIndicators(10.0, 90.0, 50.0, 50.0, 50.0, 50.0)
        self.assertGreater(spread.dispersion(), flat.dispersion())
        self.assertGreater(ins.find_example_profile("EXAMPLE-POLARIZED").indicators.dispersion(),
                           mid_profile().indicators.dispersion())

    def test_invalid_indicators_are_rejected(self):
        for values in ((101.0, 50.0, 50.0, 50.0, 50.0, 50.0),
                       (-1.0, 50.0, 50.0, 50.0, 50.0, 50.0),
                       (float("nan"), 50.0, 50.0, 50.0, 50.0, 50.0),
                       (float("inf"), 50.0, 50.0, 50.0, 50.0, 50.0)):
            with self.subTest(values=values), self.assertRaises(ins.InstitutionError):
                ins.InstitutionalIndicators(*values)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionalIndicators.from_mapping({"voice": 50.0})
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionalIndicators.from_mapping({"voice": 50.0, "unknown": 1.0})
        with self.assertRaises(ins.InstitutionError):
            ins.institutional_score(None)

    def test_invalid_weights_are_rejected(self):
        with self.assertRaises(ins.InstitutionError):
            ins.normalise_weights({"voice": 1.0})
        with self.assertRaises(ins.InstitutionError):
            ins.normalise_weights({key: 0.0 for key in ins.INDICATOR_KEYS})
        with self.assertRaises(ins.InstitutionError):
            ins.normalise_weights({key: 1.0 for key in ins.INDICATOR_KEYS} | {"voice": -1.0})
        with self.assertRaises(ins.InstitutionError):
            ins.normalise_weights({**{key: 1.0 for key in ins.INDICATOR_KEYS}, "extra": 1.0})
        with self.assertRaises(ins.InstitutionError):
            ins.normalise_weights("voice=1")

    def test_raw_key_scan_detects_a_planted_key(self):
        self.assertEqual(ins.forbidden_keys({"score": 1.0, "nested": [{"ok": 2.0}]}), [])
        self.assertEqual(ins.forbidden_keys({"nested": {"raw": [1, 2]}}), ["/nested/raw"])


class OverlaySignTests(unittest.TestCase):
    def test_status_quo_is_the_control_line(self):
        report = overlay("STATUS-QUO")
        full = report["overlay"]["full_effect"]
        self.assertAlmostEqual(full["delta_score_points"], 0.0)
        self.assertAlmostEqual(full["delta_dispersion"], 0.0)
        self.assertAlmostEqual(full["risk_premium"]["delta_bps"], 0.0)
        self.assertAlmostEqual(full["investment"]["delta_pp"], 0.0)
        self.assertAlmostEqual(full["growth"]["delta_pp"], 0.0)
        for row in report["overlay"]["path"]:
            self.assertAlmostEqual(row["delta_score_points"], 0.0)

    def test_reform_improves_quality_and_lowers_the_premium(self):
        report = overlay("REFORM")
        full = report["overlay"]["full_effect"]
        self.assertGreater(full["delta_score_points"], 0.0)
        self.assertLess(full["risk_premium"]["delta_bps"], 0.0)
        self.assertGreater(full["investment"]["delta_pp"], 0.0)
        self.assertGreater(full["growth"]["delta_pp"], 0.0)
        self.assertEqual(full["risk_premium"]["score_component_bps"],
                         ins.OverlayParameters().beta_bps_per_point * full["delta_score_points"] * -1)

    def test_deterioration_lowers_quality_and_raises_the_premium(self):
        report = overlay("DETERIORATION")
        full = report["overlay"]["full_effect"]
        self.assertLess(full["delta_score_points"], 0.0)
        self.assertGreater(full["risk_premium"]["delta_bps"], 0.0)
        self.assertLess(full["investment"]["delta_pp"], 0.0)
        self.assertLess(full["growth"]["delta_pp"], 0.0)

    def test_polarization_adds_a_dispersion_premium(self):
        report = overlay("POLARIZATION")
        full = report["overlay"]["full_effect"]
        self.assertGreater(full["delta_dispersion"], 0.0)
        score_only = (ins.OverlayParameters().beta_bps_per_point * -full["delta_score_points"])
        self.assertGreater(full["risk_premium"]["delta_bps"], score_only)
        self.assertGreater(full["risk_premium"]["dispersion_component_bps"], 0.0)
        # The score loss is smaller than under deterioration, the premium is not proportional to it.
        deterioration = overlay("DETERIORATION")["overlay"]["full_effect"]
        self.assertGreater(full["delta_score_points"], deterioration["delta_score_points"])
        self.assertGreater(deterioration["risk_premium"]["delta_bps"],
                           full["risk_premium"]["delta_bps"])

    def test_magnitude_shocks_are_monotone(self):
        small = next(item for item in ins.default_scenarios()
                     if item.scenario_id == "DETERIORATION")
        big = replace(small, magnitude=2.0)
        base = overlay("DETERIORATION")["overlay"]["full_effect"]
        stronger = ins.scenario_overlay(mid_profile(), big)["overlay"]["full_effect"]
        self.assertLess(stronger["delta_score_points"], base["delta_score_points"])
        self.assertGreater(stronger["risk_premium"]["delta_bps"],
                           base["risk_premium"]["delta_bps"])
        self.assertLess(stronger["investment"]["delta_pp"], base["investment"]["delta_pp"])
        reform = next(item for item in ins.default_scenarios() if item.scenario_id == "REFORM")
        stronger_reform = ins.scenario_overlay(mid_profile(),
                                               replace(reform, magnitude=1.5))
        self.assertGreater(stronger_reform["overlay"]["full_effect"]["delta_score_points"],
                           overlay("REFORM")["overlay"]["full_effect"]["delta_score_points"])

    def test_premium_floor_and_cap(self):
        big_reform = ins.InstitutionScenario(
            "BIG-REFORM", "Большая реформа", "reform", {key: 90.0 for key in ins.INDICATOR_KEYS})
        floor = ins.scenario_overlay(ins.find_example_profile("EXAMPLE-LOW"), big_reform)
        self.assertGreaterEqual(floor["overlay"]["full_effect"]["risk_premium"]["level_bps"], 0.0)
        self.assertTrue(floor["overlay"]["full_effect"]["risk_premium"]["clipped"])

        capped = ins.scenario_overlay(
            ins.find_example_profile("EXAMPLE-HIGH"),
            next(item for item in ins.default_scenarios()
                 if item.scenario_id == "DETERIORATION"),
            ins.OverlayParameters(base_risk_premium_bps=50.0, premium_cap_bps=100.0,
                                  beta_bps_per_point=40.0))
        premium = capped["overlay"]["full_effect"]["risk_premium"]
        self.assertAlmostEqual(premium["level_bps"], 100.0)
        self.assertTrue(premium["clipped"])
        self.assertTrue(capped["checks"]["premium_non_negative"])

    def test_investment_stays_inside_the_unit_interval(self):
        scenario = next(item for item in ins.default_scenarios()
                        if item.scenario_id == "DETERIORATION")
        low = ins.scenario_overlay(mid_profile(), scenario,
                                   ins.OverlayParameters(base_investment_ratio=0.001))
        self.assertAlmostEqual(low["overlay"]["full_effect"]["investment"]["level_ratio"], 0.0)
        self.assertTrue(low["checks"]["investment_bounds"])
        with self.assertRaises(ins.InstitutionError):
            ins.OverlayParameters(base_investment_ratio=1.5)

    def test_indicator_clipping_is_reported(self):
        scenario = ins.InstitutionScenario(
            "CLIP-UP", "Сдвиг с ограничением шкалой", "reform",
            {key: 90.0 for key in ins.INDICATOR_KEYS})
        full = ins.scenario_overlay(ins.find_example_profile("EXAMPLE-LOW"),
                                    scenario)["overlay"]["full_effect"]
        self.assertTrue(full["clipped"])
        # The composite cannot gain more than the headroom to 100, so applied < requested here.
        self.assertLess(full["applied_delta_points"], full["requested_delta_points"])
        self.assertGreater(full["applied_delta_points"], 0.0)
        self.assertLessEqual(full["score"], 100.0)


class PathTests(unittest.TestCase):
    def test_ramp_is_linear_and_converges(self):
        report = overlay("REFORM")  # ramp_years = 4, horizon = 5
        path = report["overlay"]["path"]
        self.assertEqual([round(row["ramp"], 6) for row in path], [0.25, 0.5, 0.75, 1.0, 1.0])
        self.assertTrue(report["overlay"]["converged"])
        self.assertAlmostEqual(path[0]["score"],
                               ins.institutional_score(
                                   mid_profile().indicators.shifted(
                                       next(item for item in ins.default_scenarios()
                                            if item.scenario_id == "REFORM").effective_deltas,
                                       ramp=0.25),
                                   mid_profile().weights))
        scores = [row["score"] for row in path]
        self.assertEqual(scores, sorted(scores))

    def test_short_horizon_does_not_converge(self):
        report = overlay("REFORM", horizon_years=2)
        self.assertFalse(report["overlay"]["converged"])
        self.assertAlmostEqual(report["overlay"]["path"][-1]["ramp"], 0.5)
        self.assertGreater(report["overlay"]["full_effect"]["score"],
                           report["overlay"]["path"][-1]["score"])

    def test_path_premium_follows_the_score(self):
        path = overlay("DETERIORATION")["overlay"]["path"]
        premiums = [row["risk_premium_bps"] for row in path]
        self.assertEqual(premiums, sorted(premiums))
        self.assertGreater(premiums[-1], premiums[0])

    def test_horizon_and_parameters_are_validated(self):
        for kwargs in ({"horizon_years": 0}, {"horizon_years": ins.MAX_HORIZON + 1},
                       {"beta_bps_per_point": -1.0}, {"premium_cap_bps": 0.0},
                       {"base_risk_premium_bps": 5000.0}, {"base_investment_ratio": -0.1},
                       {"elasticity_investment_pp_per_point": -0.1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ins.InstitutionError):
                ins.OverlayParameters(**kwargs)


class ScenarioValidationTests(unittest.TestCase):
    def test_scenario_boundaries(self):
        good = {key: 0.0 for key in ins.INDICATOR_KEYS}
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("bad-id", "x", "reform", good)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "", "reform", good)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "unknown", good)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "reform", good, magnitude=0.0)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "reform", good, magnitude=11.0)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "reform", good, ramp_years=0)
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "reform", {**good, "voice": 101.0})
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "reform", {**good, "unknown": 1.0})
        with self.assertRaises(ins.InstitutionError):
            ins.InstitutionScenario("OK-ID", "x", "reform", {**good, "voice": 60.0},
                                    magnitude=2.0)

    def test_config_helpers_fail_closed(self):
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_config({"name": "x"})
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_config({"indicators": {key: 50.0 for key in ins.INDICATOR_KEYS},
                                     "unknown": 1})
        with self.assertRaises(ins.InstitutionError):
            ins.scenarios_from_config([])
        with self.assertRaises(ins.InstitutionError):
            ins.scenarios_from_config("not a list")
        with self.assertRaises(ins.InstitutionError):
            ins.parameters_from_config({"unknown": 1})
        profile = ins.profile_from_config({
            "name": "MY-PROFILE",
            "source": "USER_INPUT",
            "indicators": {key: 60.0 for key in ins.INDICATOR_KEYS},
        })
        self.assertAlmostEqual(profile.score(), 60.0)
        self.assertEqual(profile.source, "USER_INPUT")

    def test_duplicate_scenario_ids_are_rejected(self):
        payload = [{"scenario_id": "SAME", "title_ru": "a", "kind": "reform",
                    "deltas": {key: 1.0 for key in ins.INDICATOR_KEYS}},
                   {"scenario_id": "SAME", "title_ru": "b", "kind": "reform",
                    "deltas": {key: 1.0 for key in ins.INDICATOR_KEYS}}]
        with self.assertRaises(ins.InstitutionError):
            ins.scenarios_from_config(payload)

    def test_indicator_text_parser(self):
        text = ",".join(f"{key}={index * 10}" for index, key in enumerate(ins.INDICATOR_KEYS))
        values = ins.parse_indicator_text(text)
        self.assertEqual(values, {key: index * 10 for index, key in enumerate(ins.INDICATOR_KEYS)})
        with self.assertRaises(ins.InstitutionError):
            ins.parse_indicator_text("voice=50")
        with self.assertRaises(ins.InstitutionError):
            ins.parse_indicator_text("voice=50,unknown=10")
        with self.assertRaises(ins.InstitutionError):
            ins.parse_indicator_text("voice=50,voice=60")
        with self.assertRaises(ins.InstitutionError):
            ins.parse_indicator_text("voice=101")
        with self.assertRaises(ins.InstitutionError):
            ins.parse_indicator_text("voice 50")
        with self.assertRaises(ins.InstitutionError):
            ins.parse_indicator_text("")


class ModifierTests(unittest.TestCase):
    def test_modifier_exports_three_channels_and_no_gating(self):
        report = overlay("DETERIORATION")
        modifier = ins.as_scenario_modifier(report)
        self.assertEqual(modifier["kind"], "institutional_modifier")
        self.assertIs(modifier["gating"], False)
        self.assertIs(modifier["causal_claim"], False)
        self.assertIs(modifier["not_calibrated"], True)
        self.assertEqual(modifier["scenario_id"], "DETERIORATION")
        self.assertGreater(modifier["risk_premium_bps"], 0.0)
        self.assertLess(modifier["investment_ratio_delta"], 0.0)
        self.assertAlmostEqual(modifier["investment_ratio_delta"] * 100,
                               modifier["investment_delta_pp"])
        self.assertIn("block_16_geopolitics", modifier["applies_to"])
        self.assertEqual(modifier["data_status"], ins.DATA_STATUS)

    def test_modifier_requires_a_scenario_report(self):
        with self.assertRaises(ins.InstitutionError):
            ins.as_scenario_modifier({"schema": "something-else"})
        with self.assertRaises(ins.InstitutionError):
            ins.as_scenario_modifier(None)


class RunAllTests(unittest.TestCase):
    def test_four_scenarios_in_declared_order(self):
        result = fixture()
        self.assertEqual([entry["id"] for entry in result["index"]["scenarios"]],
                         ["STATUS-QUO", "REFORM", "DETERIORATION", "POLARIZATION"])
        for report in result["reports"]:
            self.assertEqual(report["schema"], ins.SCENARIO_SCHEMA)
            self.assertEqual(report["status"], ins.STATUS)
            self.assertIs(report["admission"], False)
            self.assertIs(report["gating"], False)
            self.assertIs(report["causal_claim"], False)
            self.assertIs(report["not_calibrated"], True)
            self.assertEqual(report["data_status"], "ASSUMPTION_BASED")
            self.assertEqual(report["provenance"]["country_table"], False)
            for name in ("score_bounds", "indicator_bounds", "weights_normalised",
                         "premium_non_negative", "investment_bounds", "path_monotone_ramp",
                         "no_raw_data"):
                self.assertTrue(report["checks"][name], name)

    def test_examples_are_labelled_illustrative(self):
        result = fixture()
        self.assertEqual(result["index"]["profile"]["source"], "ILLUSTRATIVE")
        self.assertTrue(result["index"]["profile"]["not_calibrated"])
        self.assertIn("не страновые данные", result["index"]["profile"]["disclaimer"])
        for profile in ins.example_profiles():
            self.assertEqual(profile.source, "ILLUSTRATIVE")
            self.assertTrue(profile.name.startswith("EXAMPLE-"))
        with self.assertRaises(ins.InstitutionError):
            ins.find_example_profile("Germany")
        with self.assertRaises(ins.InstitutionError):
            ins.find_example_profile("")

    def test_only_selects_a_subset_case_insensitively(self):
        result = ins.run_all(only=("reform",))
        self.assertEqual([entry["id"] for entry in result["index"]["scenarios"]], ["REFORM"])
        with self.assertRaises(ins.InstitutionError):
            ins.run_all(only=("no-such-scenario",))
        with self.assertRaises(ins.InstitutionError):
            ins.run_all(only=())

    def test_two_runs_are_byte_identical(self):
        first = ins.run_all()
        second = ins.run_all()
        self.assertEqual(ins.canonical_json(first), ins.canonical_json(second))
        self.assertEqual(first["index"]["digest"], second["index"]["digest"])

    def test_digest_tracks_a_parameter(self):
        baseline = ins.run_all()
        changed = ins.run_all(parameters=ins.OverlayParameters(beta_bps_per_point=5.0))
        self.assertNotEqual(baseline["index"]["digest"], changed["index"]["digest"])
        reports_a = by_id(baseline)["REFORM"]["overlay"]["full_effect"]["risk_premium"]
        reports_b = by_id(changed)["REFORM"]["overlay"]["full_effect"]["risk_premium"]
        self.assertNotEqual(reports_a["delta_bps"], reports_b["delta_bps"])

    def test_reports_contain_no_raw_data_and_no_non_finite_numbers(self):
        result = fixture()
        self.assertEqual(ins.forbidden_keys(result), [])
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("NaN", payload)
        self.assertNotIn("Infinity", payload)
        self.assertIn("assumptions", result["index"])
        self.assertIn("limitations", result["index"])
        self.assertTrue(result["index"]["checks"]["all_no_raw_data"])


class BundleTests(unittest.TestCase):
    def test_user_bundle_is_loaded_and_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps({
                "name": "USER-EXAMPLE",
                "indicators": {key: 70.0 for key in ins.INDICATOR_KEYS},
                "parameters": {"horizon_years": 3},
                "scenarios": [{"scenario_id": "CUSTOM-SHOCK", "title_ru": "Свой шок",
                               "kind": "deterioration",
                               "deltas": {key: -5.0 for key in ins.INDICATOR_KEYS}}],
            }, ensure_ascii=False), encoding="utf-8")
            profile, scenarios, parameters = ins.load_bundle(path)
            self.assertEqual(profile.source, "USER_INPUT")
            self.assertEqual(profile.score(), 70.0)
            self.assertEqual([item.scenario_id for item in scenarios], ["CUSTOM-SHOCK"])
            self.assertEqual(parameters.horizon_years, 3)
            with self.assertRaises(ins.InstitutionError):
                ins.load_profile_file(path)  # bundle, not profile-only
            with self.assertRaises(FileNotFoundError):
                ins.load_bundle(Path(tmp) / "absent.json")

    def test_broken_bundle_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ins.InstitutionError):
                ins.load_bundle(broken)
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"indicators": {key: 50.0 for key in ins.INDICATOR_KEYS},
                                       "extra_key": 1}), encoding="utf-8")
            with self.assertRaises(ins.InstitutionError):
                ins.load_bundle(bad)


class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out)])
            self.assertEqual(code, cli.EXIT_OK)
            report_json = out / "report.json"
            report_md = out / "report.md"
            self.assertTrue(report_json.is_file())
            self.assertTrue(report_md.is_file())
            for scenario_id in ("STATUS-QUO", "REFORM", "DETERIORATION", "POLARIZATION"):
                self.assertTrue((out / "reports" / f"{scenario_id}.json").is_file())
                self.assertTrue((out / "reports" / f"{scenario_id}.md").is_file())
            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], cli.REPORT_SCHEMA)
            self.assertEqual(payload["status"], ins.STATUS)
            self.assertIs(payload["admission"], False)
            self.assertIs(payload["not_calibrated"], True)
            self.assertEqual(len(payload["scenarios"]), 4)
            text = report_md.read_text(encoding="utf-8")
            for needle in ("RESEARCH_ONLY", "admission=false", "Допущения", "Ограничения",
                           "Проверки", "не допуск"):
                self.assertIn(needle, text)

    def test_cli_scenario_subset_and_country(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--scenario", "reform,polarization",
                                 "--country", "EXAMPLE-LOW"])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in payload["index"]["scenarios"]],
                             ["REFORM", "POLARIZATION"])
            self.assertEqual(payload["index"]["profile"]["name"], "EXAMPLE-LOW")
            self.assertFalse((out / "reports" / "STATUS-QUO.json").exists())

    def test_cli_indicators_override_the_profile(self):
        indicators = ",".join(f"{key}=100" for key in ins.INDICATOR_KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--indicators", indicators])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(payload["index"]["profile"]["score"], 100.0)
            self.assertEqual(payload["index"]["profile"]["source"], "USER_INPUT")

    def test_cli_demo_writes_the_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--demo"])
            self.assertEqual(code, cli.EXIT_OK)
            demo_json = out / "demo.json"
            demo_md = out / "demo.md"
            self.assertTrue(demo_json.is_file())
            self.assertTrue(demo_md.is_file())
            demo = json.loads(demo_json.read_text(encoding="utf-8"))
            self.assertEqual(demo["schema"], cli.DEMO_SCHEMA)
            self.assertEqual(len(demo["profiles"]), 4)
            self.assertEqual([item["id"] for item in demo["scenarios"]],
                             ["STATUS-QUO", "REFORM", "DETERIORATION", "POLARIZATION"])
            for row in demo["profiles"]:
                self.assertEqual(set(row["headline"]),
                                 {"STATUS-QUO", "REFORM", "DETERIORATION", "POLARIZATION"})
            self.assertIs(demo["admission"], False)
            text = demo_md.read_text(encoding="utf-8")
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("не страны", text)

    def test_cli_json_flag_is_console_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inst"
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--json", "--scenario", "REFORM"])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["schema"], ins.INDEX_SCHEMA)
            self.assertEqual(len(payload["scenarios"]), 1)

    def test_cli_uses_a_user_json_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user.json"
            path.write_text(json.dumps({
                "name": "USER-1",
                "indicators": {key: 40.0 for key in ins.INDICATOR_KEYS},
            }), encoding="utf-8")
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--country", str(path)])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["index"]["profile"]["name"], "USER-1")
            self.assertEqual(payload["index"]["profile"]["source"], "USER_INPUT")
            self.assertAlmostEqual(payload["index"]["profile"]["score"], 40.0)

    def test_cli_fail_closed(self):
        cases = [
            (["--country", "NO-SUCH-PROFILE"], cli.EXIT_INVALID),
            (["--scenario", "no-such-scenario"], cli.EXIT_INVALID),
            (["--indicators", "voice=50"], cli.EXIT_INVALID),
            (["--indicators", "voice=500,stability=5,effectiveness=5,regulatory=5,"
                              "rule_of_law=5,corruption=5"], cli.EXIT_INVALID),
            (["--horizon", "0"], cli.EXIT_INVALID),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "inst"
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = cli.main(["--out", str(out)] + argv)
                self.assertEqual(code, expected)
                self.assertFalse(out.exists())

    def test_cli_missing_profile_file_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--country",
                                 str(Path(tmp) / "absent.json")])
            self.assertEqual(code, cli.EXIT_BLOCKED)
            self.assertFalse(out.exists())

    def test_cli_broken_json_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            out = Path(tmp) / "inst"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--country", str(path)])
            self.assertEqual(code, cli.EXIT_INVALID)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
