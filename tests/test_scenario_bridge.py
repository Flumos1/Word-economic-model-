"""Тесты моста блоков 15/17 (docs/210): каналы, знаки, аддитивность, границы, fail-closed.

Ни данных, ни сети: оба блока — исследовательские прототипы на объявленных допущениях, поэтому
всё считается на месте. Проверяется не «правильность экономики», а инженерный контракт моста:

* пять каналов существуют, имеют единицы, веса, полярности и владельцев;
* знаки: премия против качества, инвестиции и выпуск за качеством, TFP за внедрением;
* аддитивность: индекс = сумма взвешенных вкладов, выпуск = технологическая часть + Δроста·H;
* границы: премия, инвестиции, проценты, индекс; энергия не проваливается ниже нуля;
* детерминизм: два прогона — байт-идентичный JSON и одинаковый digest;
* отсутствие сырых данных и NaN/Inf;
* fail-closed: сценарии, горизонт, веса, профиль — и CLI с кодами 0/1/2.
"""
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.world import institutions as ins  # noqa: E402
from amfe.world import scenario_bridge as sb  # noqa: E402
from amfe.world import technology as tech  # noqa: E402

import scenario_bridge as cli  # noqa: E402

HORIZON = 10
_CACHE = {}


def grid(horizon=HORIZON):
    """Полная сетка 4×4 на профиле EXAMPLE-MID (кэш: сетка не зависит от теста)."""
    if horizon not in _CACHE:
        _CACHE[horizon] = sb.build_bridge(horizon_years=horizon)
    return _CACHE[horizon]


def cases_by_id(payload):
    return {case["case_id"]: case for case in payload["cases"]}


def case(institutions_id, technology_id, horizon=HORIZON):
    return cases_by_id(grid(horizon))[f"{institutions_id}+{technology_id}"]


def single(institutions_id="REFORM", technology_id="ai_acceleration", horizon=HORIZON, **kwargs):
    return sb.build_bridge(institution_scenarios=[institutions_id],
                           technology_scenarios=[technology_id],
                           horizon_years=horizon, **kwargs)


def floats(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from floats(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from floats(value)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield float(node)


def long_numeric_lists(node, horizon, path=""):
    """Локальная копия guard'а: числовой список длиннее horizon+1 — это сырой ряд."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += long_numeric_lists(value, horizon, f"{path}/{key}")
    elif isinstance(node, (list, tuple)):
        if node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                        for item in node):
            if len(node) > horizon + 1:
                found.append(f"{path}[len={len(node)}]")
            return found
        for index, value in enumerate(node):
            found += long_numeric_lists(value, horizon, f"{path}[{index}]")
    return found


class ChannelDeclarationTests(unittest.TestCase):
    def test_five_channels_are_declared_with_units_weights_and_owners(self):
        self.assertEqual(sb.CHANNEL_IDS, ("risk_premium_bps", "investment_delta_pp",
                                          "tfp_delta_pct", "output_delta_pct",
                                          "energy_demand_delta_pct"))
        universe = sb.channel_universe()
        self.assertEqual([row["id"] for row in universe], list(sb.CHANNEL_IDS))
        for row in universe:
            self.assertTrue(row["unit"])
            self.assertTrue(row["owner"])
            self.assertGreater(row["scale"], 0.0)
            self.assertGreater(row["weight"], 0.0)
            self.assertIn(row["polarity"], (-1.0, 0.0, 1.0))
        self.assertAlmostEqual(sum(row["weight"] for row in universe), 1.0)

    def test_every_case_exposes_exactly_the_five_channels(self):
        for payload in (grid(), single()):
            for item in payload["cases"]:
                self.assertEqual(set(item["channels"]), set(sb.CHANNEL_IDS))
                self.assertEqual(set(item["channel_units"]), set(sb.CHANNEL_IDS))

    def test_polarity_makes_energy_a_reported_but_unindexed_channel(self):
        universe = {row["id"]: row for row in sb.channel_universe()}
        self.assertEqual(universe["energy_demand_delta_pct"]["polarity"], 0.0)
        self.assertFalse(universe["energy_demand_delta_pct"]["included_in_index"])
        for channel in sb.CHANNEL_IDS[:-1]:
            self.assertTrue(universe[channel]["included_in_index"])
        payload = grid()
        for item in payload["cases"]:
            self.assertEqual(item["index"]["contributions"]["energy_demand_delta_pct"], 0.0)
            self.assertAlmostEqual(item["index"]["included_weight_sum"],
                                   1.0 - sb.DEFAULT_WEIGHTS["energy_demand_delta_pct"])


class ChannelSignTests(unittest.TestCase):
    def test_control_case_is_the_zero_line(self):
        control = case("STATUS-QUO", "baseline")
        for channel in sb.CHANNEL_IDS:
            self.assertEqual(control["channels"][channel], 0.0)
        self.assertEqual(control["index"]["value"], 0.0)
        self.assertTrue(control["blocks"]["institutions"]["converged_at_horizon"])

    def test_reform_lowers_the_premium_and_raises_investment_and_output(self):
        reform = case("REFORM", "baseline")
        channels = reform["channels"]
        self.assertLess(channels["risk_premium_bps"], 0.0)
        self.assertGreater(channels["investment_delta_pp"], 0.0)
        self.assertGreater(channels["output_delta_pct"], 0.0)
        self.assertGreater(reform["index"]["value"], 0.0)
        # Величины совпадают с docs/203: −23,5 б.п. и +0,240 п.п. ВВП при полной рампе.
        self.assertAlmostEqual(channels["risk_premium_bps"], -23.48, places=2)
        self.assertAlmostEqual(channels["investment_delta_pp"], 0.24, places=3)

    def test_deterioration_reverses_the_institution_channels(self):
        bad = case("DETERIORATION", "baseline")
        self.assertGreater(bad["channels"]["risk_premium_bps"], 0.0)
        self.assertLess(bad["channels"]["investment_delta_pp"], 0.0)
        self.assertLess(bad["channels"]["output_delta_pct"], 0.0)
        self.assertLess(bad["index"]["value"], 0.0)

    def test_polarization_costs_less_quality_but_raises_the_premium(self):
        polarization = case("POLARIZATION", "baseline")
        deterioration = case("DETERIORATION", "baseline")
        self.assertLess(abs(polarization["blocks"]["institutions"]["delta_score_points"]),
                        abs(deterioration["blocks"]["institutions"]["delta_score_points"]))
        self.assertGreater(polarization["channels"]["risk_premium_bps"], 0.0)
        self.assertLess(polarization["channels"]["investment_delta_pp"], 0.0)

    def test_ai_acceleration_raises_tfp_output_and_energy(self):
        fast = case("STATUS-QUO", "ai_acceleration")
        self.assertGreater(fast["channels"]["tfp_delta_pct"], 0.0)
        self.assertGreater(fast["channels"]["output_delta_pct"], 0.0)
        self.assertGreater(fast["channels"]["energy_demand_delta_pct"], 0.0)
        # K и L экзогенны, поэтому технологическая часть выпуска равна TFP (growth accounting).
        self.assertAlmostEqual(fast["decomposition"]["output"]["technology_output_pct"],
                               fast["channels"]["tfp_delta_pct"], places=8)
        self.assertIs(fast["checks"]["identity_output_tfp"], True)

    def test_ai_slowdown_lowers_tfp_and_output(self):
        slow = case("STATUS-QUO", "ai_slowdown")
        self.assertLess(slow["channels"]["tfp_delta_pct"], 0.0)
        self.assertLess(slow["channels"]["output_delta_pct"], 0.0)
        self.assertLess(slow["index"]["value"], 0.0)

    def test_energy_transition_cuts_energy_demand_with_a_similar_output(self):
        transition = case("STATUS-QUO", "energy_transition")
        channels = transition["channels"]
        self.assertLess(channels["energy_demand_delta_pct"], 0.0)
        self.assertLess(abs(channels["energy_demand_delta_pct"]),
                        2.0 * abs(channels["output_delta_pct"]) + 5.0)
        self.assertLess(transition["blocks"]["technology"]["energy_change_pct_absolute"],
                        transition["blocks"]["technology"]["output_gain_pct_absolute"])

    def test_institution_channels_do_not_move_with_the_technology_scenario(self):
        premiums, investments = [], []
        for technology_id in ("baseline", "ai_acceleration", "ai_slowdown",
                              "energy_transition"):
            item = case("REFORM", technology_id)
            premiums.append(item["channels"]["risk_premium_bps"])
            investments.append(item["channels"]["investment_delta_pp"])
        self.assertEqual(len(set(premiums)), 1)
        self.assertEqual(len(set(investments)), 1)
        # Величина совпадает с docs/203 §4: REFORM даёт ≈ −23,5 б.п. и +0,240 п.п. ВВП.
        self.assertAlmostEqual(premiums[0], -23.4797, places=4)
        self.assertAlmostEqual(investments[0], 0.24, places=9)

    def test_technology_channels_do_not_move_with_the_institution_scenario(self):
        reference = case("STATUS-QUO", "ai_acceleration")["channels"]
        for institutions_id in ("REFORM", "DETERIORATION", "POLARIZATION"):
            other = case(institutions_id, "ai_acceleration")["channels"]
            self.assertAlmostEqual(other["tfp_delta_pct"], reference["tfp_delta_pct"], places=9)
            self.assertAlmostEqual(other["energy_demand_delta_pct"],
                                   reference["energy_demand_delta_pct"], places=9)

    def test_sign_checks_are_reported_for_every_case(self):
        for payload in (grid(), single()):
            for item in payload["cases"]:
                checks = item["checks"]
                for name in ("sign_premium_opposes_quality", "sign_investment_follows_quality",
                             "sign_tfp_follows_adoption", "sign_output_follows_parts"):
                    self.assertIs(checks[name], True, f"{item['case_id']}: {name}")


class AdditivityTests(unittest.TestCase):
    def test_output_channel_is_the_sum_of_its_parts(self):
        for payload in (grid(), single()):
            for item in payload["cases"]:
                parts = item["decomposition"]["output"]
                self.assertAlmostEqual(
                    parts["technology_output_pct"] + parts["institutions_growth_pct"],
                    item["channels"]["output_delta_pct"], places=9)
                self.assertLessEqual(abs(parts["residual_pct"]), 1e-9)
                self.assertIs(item["checks"]["additivity_output"], True)

    def test_index_equals_the_sum_of_the_weighted_contributions(self):
        for payload in (grid(), single()):
            for item in payload["cases"]:
                contributions = item["index"]["contributions"]
                self.assertAlmostEqual(sum(contributions.values()), item["index"]["value"],
                                       places=9)
                expected = sum(sb.DEFAULT_WEIGHTS[channel]
                               * sb.CHANNEL_BY_ID[channel]["polarity"]
                               * item["channels"][channel]
                               / sb.CHANNEL_BY_ID[channel]["scale"]
                               for channel in sb.CHANNEL_IDS)
                self.assertAlmostEqual(item["index"]["value"], expected, places=9)
                self.assertIs(item["checks"]["additivity_index"], True)

    def test_institutions_output_part_is_growth_times_horizon(self):
        item = case("REFORM", "baseline", horizon=7)
        growth = item["blocks"]["institutions"]["delta_growth_pp"]
        self.assertAlmostEqual(item["decomposition"]["output"]["institutions_growth_pct"],
                               growth * 7, places=9)

    def test_technology_output_equals_tfp_under_exogenous_factors(self):
        for horizon in (1, 10, 50):
            for item in grid(horizon)["cases"]:
                self.assertIs(item["checks"]["identity_output_tfp"], True, item["case_id"])
                self.assertAlmostEqual(
                    item["decomposition"]["output"]["technology_output_pct"],
                    item["channels"]["tfp_delta_pct"], places=6)
                self.assertAlmostEqual(item["channels"]["output_delta_pct"],
                                       item["channels"]["tfp_delta_pct"]
                                       + item["decomposition"]["output"]["institutions_growth_pct"],
                                       places=6)

    def test_horizon_changes_numbers_and_keeps_the_contract(self):
        growth_parts = []
        tech_channels = []
        for horizon in (1, 3, 10, 25):
            payload = single(horizon=horizon)
            item = payload["cases"][0]
            self.assertEqual(payload["horizon_years"], horizon)
            self.assertIs(item["checks"]["horizon_consistent"], True)
            self.assertAlmostEqual(
                item["decomposition"]["output"]["institutions_growth_pct"],
                item["blocks"]["institutions"]["delta_growth_pp"] * horizon, places=9)
            growth_parts.append(item["decomposition"]["output"]["institutions_growth_pct"])
            tech_channels.append(item["channels"]["tfp_delta_pct"])
        self.assertEqual(growth_parts, sorted(growth_parts))
        self.assertLess(growth_parts[0], growth_parts[-1])
        self.assertEqual(len(set(tech_channels)), len(tech_channels))

    def test_partial_ramp_is_flagged_when_the_horizon_is_shorter_than_the_ramp(self):
        payload = single(institutions_id="REFORM", horizon=2)
        institution = payload["cases"][0]["blocks"]["institutions"]
        self.assertFalse(institution["converged_at_horizon"])
        self.assertLess(institution["ramp_at_horizon"], 1.0)
        payload = single(institutions_id="REFORM", horizon=10)
        self.assertTrue(payload["cases"][0]["blocks"]["institutions"]["converged_at_horizon"])


class BoundsTests(unittest.TestCase):
    def test_channel_bounds_hold_for_the_whole_grid(self):
        for payload in (grid(), grid(1), grid(25)):
            for item in payload["cases"]:
                channels = item["channels"]
                self.assertLessEqual(abs(channels["risk_premium_bps"]),
                                     sb.MAX_ABS_RISK_PREMIUM_BPS)
                self.assertLessEqual(abs(channels["investment_delta_pp"]),
                                     sb.MAX_ABS_INVESTMENT_PP)
                for channel in ("tfp_delta_pct", "output_delta_pct",
                                "energy_demand_delta_pct"):
                    self.assertGreater(channels[channel], sb.MIN_PCT_LEVEL)
                    self.assertLessEqual(abs(channels[channel]), sb.MAX_ABS_PCT)
                self.assertLessEqual(abs(item["index"]["value"]), sb.MAX_ABS_INDEX)
                for name in ("risk_premium_bounds", "investment_bounds", "tfp_bounds",
                             "output_bounds", "energy_bounds", "levels_non_negative",
                             "index_bounds", "channels_finite"):
                    self.assertIs(item["checks"][name], True, f"{item['case_id']}: {name}")

    def test_extreme_profile_stays_inside_the_declared_ranges(self):
        low = sb.build_bridge(profile="EXAMPLE-LOW", institution_scenarios=["REFORM"],
                              technology_scenarios=["ai_acceleration"], horizon_years=5)
        high = sb.build_bridge(profile="EXAMPLE-HIGH", institution_scenarios=["DETERIORATION"],
                               technology_scenarios=["energy_transition"], horizon_years=5)
        for payload in (low, high):
            for item in payload["cases"]:
                self.assertLessEqual(abs(item["index"]["value"]), sb.MAX_ABS_INDEX)
                self.assertIs(item["checks"]["levels_non_negative"], True)

    def test_custom_weights_change_the_index_but_not_the_channels(self):
        baseline = single()
        doubled = single(weights={"risk_premium_bps": 0.4, "investment_delta_pp": 0.1,
                                  "tfp_delta_pct": 0.2, "output_delta_pct": 0.2,
                                  "energy_demand_delta_pct": 0.1})
        self.assertNotEqual(baseline["cases"][0]["index"]["value"],
                            doubled["cases"][0]["index"]["value"])
        self.assertEqual(baseline["cases"][0]["channels"], doubled["cases"][0]["channels"])
        self.assertAlmostEqual(doubled["cases"][0]["index"]["weight_sum"], 1.0)
        self.assertEqual(set(doubled["weights"]), set(sb.CHANNEL_IDS))


class HygieneTests(unittest.TestCase):
    def test_status_flags_and_provenance(self):
        payload = grid()
        self.assertEqual(payload["schema"], "amfe.world.scenario_bridge.v1")
        self.assertEqual(payload["status"], "RESEARCH_ONLY")
        self.assertIs(payload["admission"], False)
        self.assertIs(payload["gating"], False)
        self.assertIs(payload["causal_claim"], False)
        self.assertEqual(payload["data_status"], "ASSUMPTION_BASED")
        self.assertIs(payload["not_calibrated"], True)
        provenance = payload["provenance"]
        for key in ("data_read", "network", "clock", "randomness", "country_table",
                    "mrio_touched", "delta_a_applied"):
            self.assertIs(provenance[key], False)
        self.assertEqual(provenance["calibration"], "NONE")
        for item in payload["cases"]:
            self.assertIs(item["checks"]["gating"], False)
            self.assertIs(item["checks"]["causal_claim"], False)

    def test_no_raw_data_no_country_table_and_no_non_finite_numbers(self):
        for payload in (grid(), sb.demo_report(HORIZON)):
            self.assertEqual(sb.forbidden_keys(payload), [])
            self.assertEqual(long_numeric_lists(payload, payload["horizon_years"]), [])
            for value in floats(payload):
                self.assertTrue(math.isfinite(value))
            text = json.dumps(payload, ensure_ascii=False)
            for needle in ("data/mrio", "data\\mrio", "country_table\": true", "observations"):
                self.assertNotIn(needle, text)
            self.assertIs(payload["checks"]["no_raw_data"], True)

    def test_raw_key_scan_detects_a_planted_key(self):
        payload = {"case": {"channels": {"risk_premium_bps": 0.0}}, "series": [1, 2, 3]}
        found = sb.forbidden_keys(payload)
        self.assertEqual(found, ["/series"])
        self.assertTrue(long_numeric_lists({"rows": [0.0] * 40}, 10))

    def test_two_runs_are_byte_identical(self):
        first = sb.build_bridge(institution_scenarios=["REFORM", "POLARIZATION"],
                                technology_scenarios=["baseline", "ai_acceleration"],
                                horizon_years=8)
        second = sb.build_bridge(institution_scenarios=["reform", "polarization"],
                                 technology_scenarios=["baseline", "ai-acceleration"],
                                 horizon_years=8)
        self.assertEqual(sb.canonical_json(first), sb.canonical_json(second))
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(len(first["digest"]), 64)

    def test_digest_tracks_the_horizon_and_the_profile(self):
        base = single(horizon=5)["digest"]
        self.assertNotEqual(base, single(horizon=6)["digest"])
        self.assertNotEqual(base, single(horizon=5, profile="EXAMPLE-LOW")["digest"])

    def test_case_grid_and_declared_order(self):
        payload = grid()
        self.assertEqual(len(payload["cases"]), 16)
        self.assertEqual([item["case_id"] for item in payload["cases"][:4]],
                         ["STATUS-QUO+baseline", "STATUS-QUO+ai_acceleration",
                          "STATUS-QUO+ai_slowdown", "STATUS-QUO+energy_transition"])
        self.assertEqual(payload["checks"]["case_count"], 16)
        subset = sb.build_bridge(institution_scenarios=["status_quo", "reform"],
                                 technology_scenarios=["baseline"], horizon_years=4)
        self.assertEqual([item["case_id"] for item in subset["cases"]],
                         ["STATUS-QUO+baseline", "REFORM+baseline"])
        self.assertEqual(subset["inputs"]["technology"]["requested_scenario_ids"], ["baseline"])
        self.assertEqual(subset["inputs"]["technology"]["scenario_ids"], ["baseline"])

    def test_assumptions_and_limitations_are_complete(self):
        self.assertEqual([item["id"] for item in sb.ASSUMPTIONS],
                         [f"B{index}" for index in range(1, 11)])
        for item in sb.assumptions():
            self.assertTrue(item["statement"])
            self.assertTrue(item["replace_with"])
            self.assertIn(item["kind"], ("ASSUMPTION_BASED", "SCENARIO"))
        self.assertGreaterEqual(len(sb.limitations()), 6)
        payload = grid()
        self.assertEqual(len(payload["assumptions"]), 10)
        self.assertEqual(payload["limitations"], sb.limitations())
        self.assertEqual(payload["module"]["module_id"], "world.scenario_bridge")


class UpstreamConsistencyTests(unittest.TestCase):
    def test_technology_channels_reproduce_a_direct_block_17_run(self):
        report = tech.build_report(["baseline", "ai_acceleration"], None, HORIZON)
        documents = {item["scenario_id"]: item for item in report["scenarios"]}
        base = documents["baseline"]["aggregate"]["paths"]
        fast = documents["ai_acceleration"]["aggregate"]["paths"]
        expected_tfp = 100.0 * (fast["tfp_ratio"][HORIZON] / base["tfp_ratio"][HORIZON] - 1.0)
        expected_output = 100.0 * (fast["output_ratio"][HORIZON]
                                   / base["output_ratio"][HORIZON] - 1.0)
        expected_energy = 100.0 * (fast["energy_ratio"][HORIZON]
                                   / base["energy_ratio"][HORIZON] - 1.0)
        item = case("STATUS-QUO", "ai_acceleration")
        self.assertAlmostEqual(item["channels"]["tfp_delta_pct"], expected_tfp, places=9)
        self.assertAlmostEqual(item["channels"]["output_delta_pct"], expected_output, places=9)
        self.assertAlmostEqual(item["channels"]["energy_demand_delta_pct"], expected_energy,
                               places=9)

    def test_institutions_channels_reproduce_a_direct_block_15_overlay(self):
        scenario = next(item for item in ins.default_scenarios()
                        if item.scenario_id == "DETERIORATION")
        overlay = ins.scenario_overlay(ins.find_example_profile("EXAMPLE-MID"), scenario,
                                       ins.OverlayParameters(horizon_years=HORIZON))
        point = overlay["overlay"]["path"][-1]
        item = case("DETERIORATION", "baseline")
        self.assertAlmostEqual(item["channels"]["risk_premium_bps"],
                               point["delta_risk_premium_bps"], places=9)
        self.assertAlmostEqual(item["channels"]["investment_delta_pp"],
                               point["delta_investment_pp"], places=9)
        self.assertEqual(item["blocks"]["institutions"]["digest"], overlay["digest"])

    def test_demo_uses_the_declared_pairs_and_has_notes(self):
        demo = sb.demo_report(HORIZON)
        self.assertEqual([item["case_id"] for item in demo["cases"]],
                         [f"{left}+{right}" for left, right in sb.DEMO_PAIRS])
        self.assertIn("demo", demo)
        self.assertGreaterEqual(len(demo["demo"]["notes_ru"]), 4)
        self.assertEqual(demo["demo"]["horizon_years"], HORIZON)
        self.assertIs(demo["admission"], False)


class FailClosedTests(unittest.TestCase):
    def test_unknown_scenarios_are_rejected(self):
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(institution_scenarios=["NO-SUCH"])
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(technology_scenarios=["no_such"])
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(institution_scenarios=[])
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(technology_scenarios=[])

    def test_invalid_horizon_is_rejected(self):
        for value in (0, -1, 51, 10.5, True, "10"):
            with self.subTest(value=value), self.assertRaises(sb.BridgeError):
                sb.build_bridge(horizon_years=value)

    def test_invalid_weights_are_rejected(self):
        partial = {"risk_premium_bps": 1.0}
        unknown = dict(sb.DEFAULT_WEIGHTS, extra=0.0)
        zero = {channel: 0.0 for channel in sb.CHANNEL_IDS}
        negative = dict(sb.DEFAULT_WEIGHTS)
        negative["tfp_delta_pct"] = -0.1
        for weights in (partial, unknown, zero, negative):
            with self.subTest(weights=weights), self.assertRaises(sb.BridgeError):
                sb.build_bridge(weights=weights)

    def test_invalid_pairs_are_rejected(self):
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(pairs=[("REFORM",)])
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(institution_scenarios=["REFORM"],
                            technology_scenarios=["ai_acceleration"],
                            pairs=[("REFORM", "energy_transition")])
        with self.assertRaises(sb.BridgeError):
            sb.build_bridge(institution_scenarios=[], pairs=[])

    def test_profile_resolution_and_user_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user.json"
            path.write_text(json.dumps({
                "name": "USER-BRIDGE",
                "indicators": {key: 40.0 for key in ins.INDICATOR_KEYS},
            }), encoding="utf-8")
            payload = sb.build_bridge(profile=str(path), institution_scenarios=["REFORM"],
                                      technology_scenarios=["baseline"], horizon_years=5)
            self.assertEqual(payload["profile"]["name"], "USER-BRIDGE")
            self.assertEqual(payload["profile"]["source"], "USER_INPUT")
            self.assertIs(payload["profile"]["user_supplied"], True)
            self.assertAlmostEqual(payload["profile"]["score"], 40.0)
            with self.assertRaises(FileNotFoundError):
                sb.build_bridge(profile=str(Path(tmp) / "absent.json"))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ins.InstitutionError):
                sb.build_bridge(profile=str(broken))
        with self.assertRaises(ins.InstitutionError):
            sb.build_bridge(profile="NO-SUCH-PROFILE")


class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--institutions-scenario", "reform",
                                 "--technology-scenario", "ai_acceleration", "--horizon", "10"])
            self.assertEqual(code, cli.EXIT_OK)
            report_json = out / "report.json"
            report_md = out / "report.md"
            self.assertTrue(report_json.is_file())
            self.assertTrue(report_md.is_file())
            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], cli.REPORT_SCHEMA)
            self.assertEqual(payload["schema"], sb.SCHEMA)
            self.assertEqual(payload["status"], "RESEARCH_ONLY")
            self.assertIs(payload["admission"], False)
            self.assertEqual(len(payload["cases"]), 1)
            self.assertEqual(payload["cases"][0]["case_id"], "REFORM+ai_acceleration")
            text = report_md.read_text(encoding="utf-8")
            for needle in ("RESEARCH_ONLY", "admission=false", "Каналы", "Кейсы", "Допущения",
                           "Ограничения", "Проверки", "не прогноз", "digest"):
                self.assertIn(needle, text)
            self.assertIn(payload["digest"], text)

    def test_cli_subset_builds_the_cross_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--institutions-scenario",
                                 "status_quo,reform", "--technology-scenario",
                                 "baseline,energy_transition", "--horizon", "6"])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual([item["case_id"] for item in payload["cases"]],
                             ["STATUS-QUO+baseline", "STATUS-QUO+energy_transition",
                              "REFORM+baseline", "REFORM+energy_transition"])
            self.assertEqual(payload["horizon_years"], 6)

    def test_cli_json_prints_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge"
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--json", "--profile", "EXAMPLE-LOW",
                                 "--institutions-scenario", "reform",
                                 "--technology-scenario", "ai_slowdown"])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertFalse(out.exists())
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["schema"], sb.SCHEMA)
            self.assertEqual(payload["profile"]["name"], "EXAMPLE-LOW")
            self.assertEqual(payload["cases"][0]["case_id"], "REFORM+ai_slowdown")

    def test_cli_demo_writes_demo_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--demo"])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertTrue((out / "demo.json").is_file())
            self.assertTrue((out / "demo.md").is_file())
            self.assertFalse((out / "report.json").exists())
            demo = json.loads((out / "demo.json").read_text(encoding="utf-8"))
            self.assertEqual(len(demo["cases"]), len(sb.DEMO_PAIRS))
            text = (out / "demo.md").read_text(encoding="utf-8")
            for needle in ("не прогноз", "RESEARCH_ONLY", "Как читать"):
                self.assertIn(needle, text)

    def test_cli_fail_closed_returns_one_and_writes_nothing(self):
        cases = [
            ["--institutions-scenario", "no-such"],
            ["--technology-scenario", "no-such"],
            ["--horizon", "0"],
            ["--horizon", "51"],
            ["--profile", "NO-SUCH-PROFILE"],
            ["--institutions-scenario", ""],
        ]
        for argv in cases:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "bridge"
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = cli.main(["--out", str(out)] + argv)
                self.assertEqual(code, cli.EXIT_INVALID)
                self.assertFalse(out.exists())

    def test_cli_missing_profile_file_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bridge"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--out", str(out), "--profile",
                                 str(Path(tmp) / "absent.json")])
            self.assertEqual(code, cli.EXIT_BLOCKED)
            self.assertFalse(out.exists())

    def test_cli_two_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            for out in (first, second):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = cli.main(["--out", str(out), "--institutions-scenario", "reform",
                                     "--technology-scenario", "ai_acceleration",
                                     "--horizon", "10"])
                self.assertEqual(code, cli.EXIT_OK)
            for name in ("report.json", "report.md"):
                self.assertEqual(hashlib.sha256((first / name).read_bytes()).hexdigest(),
                                 hashlib.sha256((second / name).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
