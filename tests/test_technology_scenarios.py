"""Tests for block 17: logistic adoption -> TFP -> output/energy and its CLI (docs/204).

Offline and assumption-based by construction: the module reads no data, no network and no clock, so
every test here exercises declared parameters, the logistic bounds and monotonicity, the scenario
directions, the growth-accounting identity, the energy arithmetic, determinism, the aggregate-only
output and the fail-closed paths of :mod:`amfe.world.technology` and
``tools/technology_scenarios.py``.

Run from the repository root: ``python tests/test_technology_scenarios.py``.
"""
from __future__ import annotations

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

from amfe.world import technology  # noqa: E402
from amfe.world.technology import (  # noqa: E402
    ADMISSION,
    CALIBRATION,
    DATA_CLASS,
    DEFAULT_PROFILES,
    DEMO_HORIZON_YEARS,
    SCENARIOS,
    SECTORS,
    STATUS,
    TechnologyError,
    TechnologyScenario,
    build_report,
    demo_report,
    half_time,
    logistic_level,
    logistic_path,
    parse_overrides,
)

import technology_scenarios  # noqa: E402

SCENARIO_IDS = ("baseline", "ai_acceleration", "ai_slowdown", "energy_transition")


def numeric_lists(node, path: str = "$") -> list[tuple[int, str]]:
    """Every list of numbers in a document, with its length and path (aggregate-only audit)."""
    found: list[tuple[int, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(numeric_lists(value, f"{path}.{key}"))
    elif isinstance(node, list):
        if node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                        for item in node):
            found.append((len(node), path))
        for index, value in enumerate(node):
            found.extend(numeric_lists(value, f"{path}[{index}]"))
    return found


def canonical_digest(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "digest"}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# 1. Logistic diffusion
# ---------------------------------------------------------------------------------------
class LogisticTests(unittest.TestCase):
    def test_level_starts_at_start_and_is_bounded(self):
        level = logistic_level(0.08, 0.65, 0.28, 0)
        self.assertAlmostEqual(level, 0.08, places=12)
        for period in (1, 5, 40):
            value = logistic_level(0.08, 0.65, 0.28, period)
            self.assertGreater(value, 0.08)
            self.assertLess(value, 0.65)  # asymptotic saturation, never exactly the ceiling
        self.assertLessEqual(logistic_level(0.08, 0.65, 0.28, 400), 0.65)

    def test_level_is_strictly_increasing(self):
        path = logistic_path(0.10, 0.80, 0.32, 40)
        for index in range(len(path) - 1):
            self.assertGreater(path[index + 1], path[index])

    def test_level_approaches_ceiling_from_below(self):
        far = logistic_level(0.10, 0.80, 0.32, 100)
        self.assertGreater(far, 0.80 * 0.9999)
        self.assertLess(far, 0.80)

    def test_half_time_matches_closed_form(self):
        start, ceiling, speed = 0.10, 0.80, 0.32
        time = half_time(start, ceiling, speed)
        self.assertAlmostEqual(logistic_level(start, ceiling, speed, time),
                               0.5 * ceiling, places=12)
        # the analytic half-time is the crossing of the closed form, not a fitted quantity
        self.assertAlmostEqual(time, math.log((ceiling - start) / start) / speed, places=12)

    def test_path_length_and_endpoints(self):
        path = logistic_path(0.05, 0.55, 0.22, 12)
        self.assertEqual(len(path), 13)
        self.assertAlmostEqual(path[0], 0.05, places=12)
        self.assertAlmostEqual(path[-1], logistic_level(0.05, 0.55, 0.22, 12), places=12)
        self.assertEqual(list(path), sorted(path))

    def test_invalid_parameters_fail_closed(self):
        cases = [
            (0.05, 1.5, 0.2, 5),  # ceiling above one is meaningless for a share
            (0.05, 0.0, 0.2, 5),  # ceiling must be positive
            (0.05, 0.6, 0.0, 5),  # speed must be positive
            (0.0, 0.6, 0.2, 5),   # start must be strictly positive
            (0.6, 0.6, 0.2, 5),   # start must be below the ceiling
            (0.7, 0.6, 0.2, 5),
        ]
        for start, ceiling, speed, period in cases:
            with self.assertRaises(TechnologyError):
                logistic_level(start, ceiling, speed, period)
        with self.assertRaises(TechnologyError):
            logistic_level(0.05, 0.6, 0.2, -1.0)

    def test_path_rejects_out_of_range_horizon(self):
        for years in (0, -3, technology.MAX_HORIZON_YEARS + 1):
            with self.assertRaises(TechnologyError):
                logistic_path(0.05, 0.6, 0.2, years)


# ---------------------------------------------------------------------------------------
# 2. Scenarios
# ---------------------------------------------------------------------------------------
class ScenarioTests(unittest.TestCase):
    def test_four_scenarios_are_declared(self):
        self.assertEqual(tuple(SCENARIOS), SCENARIO_IDS)
        for scenario_id, scenario in SCENARIOS.items():
            self.assertEqual(scenario.scenario_id, scenario_id)
            self.assertTrue(scenario.title_ru)
            self.assertTrue(scenario.description)
            self.assertGreater(scenario.speed_multiplier, 0.0)

    def test_acceleration_above_baseline_above_slowdown(self):
        report = build_report(list(SCENARIO_IDS), None, 15)
        documents = {item["scenario_id"]: item for item in report["scenarios"]}
        base = documents["baseline"]["aggregate"]["horizon"]
        fast = documents["ai_acceleration"]["aggregate"]["horizon"]
        slow = documents["ai_slowdown"]["aggregate"]["horizon"]
        self.assertGreater(fast["adoption"], base["adoption"])
        self.assertGreater(base["adoption"], slow["adoption"])
        self.assertGreater(fast["output_gain_pct"], base["output_gain_pct"])
        self.assertGreater(base["output_gain_pct"], slow["output_gain_pct"])
        for sector in SECTORS:
            fast_sector = documents["ai_acceleration"]["sectors"][sector]["horizon"]
            base_sector = documents["baseline"]["sectors"][sector]["horizon"]
            slow_sector = documents["ai_slowdown"]["sectors"][sector]["horizon"]
            self.assertGreater(fast_sector["adoption"], base_sector["adoption"], sector)
            self.assertGreater(base_sector["adoption"], slow_sector["adoption"], sector)

    def test_slowdown_lowers_the_effective_ceiling(self):
        report = build_report(["baseline", "ai_slowdown"], ["services"], 5)
        documents = {item["scenario_id"]: item for item in report["scenarios"]}
        base_ceiling = documents["baseline"]["sectors"]["services"]["profile"][
            "ceiling_effective"]
        slow_ceiling = documents["ai_slowdown"]["sectors"]["services"]["profile"][
            "ceiling_effective"]
        self.assertAlmostEqual(slow_ceiling, 0.85 * base_ceiling, places=12)
        # the ceiling is a share of the potential, so it can never exceed one
        self.assertLessEqual(slow_ceiling, 1.0)
        self.assertLessEqual(
            documents["ai_slowdown"]["sectors"]["services"]["horizon"]["adoption"], slow_ceiling)

    def test_ceiling_multiplier_is_capped_at_one(self):
        scenario = TechnologyScenario(scenario_id="cap", title_ru="t", description="d",
                                      ceiling_multiplier=1.5)
        adjusted = scenario.apply(DEFAULT_PROFILES["services"])
        self.assertAlmostEqual(adjusted.adoption.ceiling, 1.0, places=12)
        self.assertAlmostEqual(adjusted.adoption.speed,
                               DEFAULT_PROFILES["services"].adoption.speed, places=12)

    def test_energy_transition_uses_less_energy_than_baseline(self):
        report = build_report(["baseline", "energy_transition"], None, 15)
        documents = {item["scenario_id"]: item for item in report["scenarios"]}
        base = documents["baseline"]["aggregate"]["horizon"]
        transition = documents["energy_transition"]["aggregate"]["horizon"]
        self.assertLess(transition["energy_change_pct"], base["energy_change_pct"])
        self.assertLess(transition["energy_intensity_change_pct"],
                        base["energy_intensity_change_pct"])
        for sector in ("manufacturing", "services", "energy"):
            self.assertLess(
                documents["energy_transition"]["sectors"][sector]["horizon"]["energy_change_pct"],
                documents["baseline"]["sectors"][sector]["horizon"]["energy_change_pct"], sector)

    def test_energy_transition_diffuses_slightly_slower(self):
        report = build_report(["baseline", "energy_transition"], ["services"], 15)
        documents = {item["scenario_id"]: item for item in report["scenarios"]}
        self.assertLess(documents["energy_transition"]["sectors"]["services"]["horizon"]["adoption"],
                        documents["baseline"]["sectors"]["services"]["horizon"]["adoption"])

    def test_unknown_scenario_and_sector_fail_closed(self):
        with self.assertRaises(TechnologyError):
            build_report(["nope"], None, 5)
        with self.assertRaises(TechnologyError):
            build_report(None, ["nope"], 5)
        with self.assertRaises(TechnologyError):
            build_report([], None, 5)
        with self.assertRaises(TechnologyError):
            build_report(None, [], 5)


# ---------------------------------------------------------------------------------------
# 3. Growth accounting
# ---------------------------------------------------------------------------------------
class GrowthAccountingTests(unittest.TestCase):
    def test_identity_holds_for_every_sector_and_scenario(self):
        report = build_report(list(SCENARIO_IDS), None, 15)
        for item in report["scenarios"]:
            for sector, outcome in item["sectors"].items():
                contributions = outcome["horizon"]["contributions_log"]
                residual = abs(contributions["total"]
                               - math.log(outcome["paths"]["output_ratio"][-1]))
                self.assertLessEqual(residual, 1e-9, f"{item['scenario_id']}/{sector}")
                self.assertLessEqual(outcome["horizon"]["identity_residual"], 1e-9)
            aggregate = item["aggregate"]["horizon"]
            self.assertLessEqual(abs(aggregate["contributions_log"]["total"]
                                     - math.log(item["aggregate"]["paths"]["output_ratio"][-1])),
                                 1e-9)

    def test_zero_tfp_elasticity_leaves_tfp_unchanged(self):
        report = build_report(["ai_acceleration"], ["services"], 10,
                              {"tfp_elasticity.services": 0.0})
        outcome = report["scenarios"][0]["sectors"]["services"]
        self.assertEqual(set(outcome["paths"]["tfp_ratio"]), {1.0})
        self.assertAlmostEqual(outcome["horizon"]["tfp_gain_pct"], 0.0, places=12)
        # output still grows, but only through the exogenous capital and labour paths
        self.assertGreater(outcome["horizon"]["output_gain_pct"], 0.0)

    def test_capital_growth_raises_output(self):
        without = build_report(["baseline"], ["services"], 10, {"capital_growth": 0.0})
        with_growth = build_report(["baseline"], ["services"], 10, {"capital_growth": 0.03})
        low = without["scenarios"][0]["sectors"]["services"]["horizon"]["output_gain_pct"]
        high = with_growth["scenarios"][0]["sectors"]["services"]["horizon"]["output_gain_pct"]
        self.assertGreater(high, low)
        self.assertAlmostEqual(
            with_growth["scenarios"][0]["sectors"]["services"]["horizon"]["contributions_log"][
                "tfp"],
            without["scenarios"][0]["sectors"]["services"]["horizon"]["contributions_log"]["tfp"],
            places=12)

    def test_labour_growth_raises_output_through_its_share(self):
        zero = build_report(["baseline"], ["agriculture"], 10, {"labour_growth": 0.0})
        more = build_report(["baseline"], ["agriculture"], 10, {"labour_growth": 0.02})
        low = zero["scenarios"][0]["sectors"]["agriculture"]["horizon"]["contributions_log"]
        high = more["scenarios"][0]["sectors"]["agriculture"]["horizon"]["contributions_log"]
        self.assertEqual(low["labour"], 0.0)
        self.assertGreater(high["labour"], 0.0)
        self.assertGreater(high["total"], low["total"])

    def test_higher_tfp_elasticity_gives_more_output(self):
        report = build_report(["baseline"], ["manufacturing"], 10)
        low = report["scenarios"][0]["sectors"]["manufacturing"]["horizon"]["output_gain_pct"]
        report = build_report(["baseline"], ["manufacturing"], 10,
                              {"tfp_elasticity.manufacturing": 0.6})
        high = report["scenarios"][0]["sectors"]["manufacturing"]["horizon"]["output_gain_pct"]
        self.assertGreater(high, low)

    def test_contributions_are_additive_at_the_horizon(self):
        report = build_report(["baseline"], ["energy"], 8)
        horizon = report["scenarios"][0]["sectors"]["energy"]["horizon"]
        contributions = horizon["contributions_log"]
        self.assertAlmostEqual(contributions["tfp"] + contributions["capital"]
                               + contributions["labour"], contributions["total"], places=9)
        self.assertAlmostEqual(horizon["identity_residual"], 0.0, places=9)


# ---------------------------------------------------------------------------------------
# 4. Energy arithmetic
# ---------------------------------------------------------------------------------------
class EnergyTests(unittest.TestCase):
    def test_energy_follows_output_when_efficiency_gain_is_zero(self):
        report = build_report(["baseline"], ["manufacturing"], 10,
                              {"energy_efficiency_gain.manufacturing": 0.0})
        outcome = report["scenarios"][0]["sectors"]["manufacturing"]
        elasticity = outcome["profile"]["energy_output_elasticity"]
        for period in range(11):
            expected = outcome["paths"]["output_ratio"][period] ** elasticity
            self.assertAlmostEqual(outcome["paths"]["energy_ratio"][period], expected, places=8)

    def test_efficiency_gain_lowers_energy(self):
        no_gain = build_report(["baseline"], ["services"], 10,
                               {"energy_efficiency_gain.services": 0.0})
        gain = build_report(["baseline"], ["services"], 10,
                            {"energy_efficiency_gain.services": 0.6})
        low = no_gain["scenarios"][0]["sectors"]["services"]["horizon"]["energy_change_pct"]
        high = gain["scenarios"][0]["sectors"]["services"]["horizon"]["energy_change_pct"]
        self.assertLess(high, low)

    def test_every_path_value_is_positive(self):
        report = build_report(list(SCENARIO_IDS), None, 20)
        for item in report["scenarios"]:
            for outcome in item["sectors"].values():
                for name in ("adoption", "tfp_ratio", "output_ratio", "energy_ratio",
                             "energy_intensity_ratio"):
                    for value in outcome["paths"][name]:
                        self.assertGreater(value, 0.0, name)

    def test_energy_intensity_declines_with_adoption(self):
        report = build_report(["baseline"], None, 15)
        aggregate = report["scenarios"][0]["aggregate"]
        self.assertLess(aggregate["paths"]["energy_intensity_ratio"][-1],
                        aggregate["paths"]["energy_intensity_ratio"][0])
        self.assertLess(aggregate["horizon"]["energy_intensity_change_pct"], 0.0)
        self.assertGreater(aggregate["horizon"]["energy_change_pct"], 0.0)


# ---------------------------------------------------------------------------------------
# 5. Report: provenance, aggregate-only, determinism
# ---------------------------------------------------------------------------------------
class ReportTests(unittest.TestCase):
    def test_status_and_provenance_are_explicit(self):
        report = build_report(None, None, 15)
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertEqual(report["status"], STATUS)
        self.assertIs(report["admission"], False)
        self.assertIs(report["admission"], ADMISSION)
        self.assertEqual(report["data_class"], "ASSUMPTION_BASED")
        self.assertEqual(report["data_class"], DATA_CLASS)
        self.assertEqual(report["calibration"], "NOT_CALIBRATED")
        self.assertEqual(report["calibration"], CALIBRATION)
        self.assertEqual(report["block"], 17)
        self.assertIn("не прогноз", report["note"])
        module = report["module"]
        self.assertEqual(module["status"], STATUS)
        self.assertIs(module["admission"], False)
        self.assertIn("docs/204", module["docs"])
        self.assertIn("test_technology_scenarios", module["tests"])

    def test_assumptions_carry_provenance_and_replacement(self):
        report = build_report(None, None, 5)
        assumptions = report["assumptions"]
        self.assertGreaterEqual(len(assumptions), 40)
        for item in assumptions:
            self.assertEqual(item["source_class"], "assumption")
            self.assertTrue(item["replace_with"])
            self.assertTrue(item["unit"])
            self.assertIn(item["name"].split(".")[0],
                          set(technology.SECTOR_PARAMETERS) | set(technology.GLOBAL_PARAMETERS))
        names = {item["name"] for item in assumptions}
        self.assertIn("capital_growth", names)
        self.assertIn("tfp_elasticity.services", names)
        self.assertIn("ceiling.energy", names)

    def test_limitations_are_present_and_honest(self):
        report = build_report(None, None, 5)
        limitations = report["limitations"]
        self.assertGreaterEqual(len(limitations), 8)
        joined = " ".join(limitations).lower()
        for token in ("калибровк", "admission", "research_only", "mrio"):
            self.assertIn(token, joined)
        self.assertIn("H-005", " ".join(limitations))

    def test_report_is_aggregate_only(self):
        report = build_report(list(SCENARIO_IDS), None, 12)
        allowed = 13  # horizon + 1 points per path
        for length, path in numeric_lists(report):
            self.assertLessEqual(length, allowed, path)
        keys = set()

        def collect(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    keys.add(str(key))
                    collect(value)
            elif isinstance(node, list):
                for value in node:
                    collect(value)

        collect(report)
        for forbidden in ("raw", "series", "country", "iso3", "source_path"):
            self.assertNotIn(forbidden, keys)
        self.assertTrue(report["checks"]["no_raw_series"])
        self.assertEqual(report["horizon_years"], 12)

    def test_determinism_same_payload_and_digest(self):
        first = build_report(["ai_acceleration"], ["services"], 9)
        second = build_report(["ai_acceleration"], ["services"], 9)
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["digest"], canonical_digest(first))

    def test_digest_changes_with_horizon_and_params(self):
        base = build_report(["baseline"], None, 10)
        longer = build_report(["baseline"], None, 12)
        other = build_report(["baseline"], None, 10, {"capital_growth": 0.01})
        self.assertNotEqual(base["digest"], longer["digest"])
        self.assertNotEqual(base["digest"], other["digest"])

    def test_sector_filter_renormalises_weights(self):
        report = build_report(["baseline"], ["services", "energy"], 6)
        aggregate = report["scenarios"][0]["aggregate"]
        self.assertEqual(aggregate["sectors_included"], ["services", "energy"])
        self.assertTrue(aggregate["weights_renormalised"])
        self.assertAlmostEqual(sum(aggregate["sector_weights"].values()), 1.0, places=12)
        self.assertTrue(aggregate["checks"]["weights_sum_to_one"])
        self.assertEqual(report["sectors"], ["services", "energy"])

    def test_weight_overrides_must_keep_the_sum(self):
        with self.assertRaises(TechnologyError):
            build_report(["baseline"], None, 5, {"output_weight.services": 0.5})
        # a consistent pair is accepted and reported
        report = build_report(["baseline"], None, 5,
                              {"output_weight.services": 0.55, "output_weight.manufacturing": 0.20})
        self.assertTrue(report["checks"]["all_aggregate_checks_pass"])

    def test_horizon_bounds(self):
        for horizon in (0, -1, technology.MAX_HORIZON_YEARS + 1, True, 10.0):
            with self.assertRaises(TechnologyError):
                build_report(None, None, horizon)
        self.assertEqual(build_report(None, None, 1)["horizon_years"], 1)
        self.assertEqual(
            build_report(None, None, technology.MAX_HORIZON_YEARS)["horizon_years"],
            technology.MAX_HORIZON_YEARS)

    def test_params_parse_and_fail_closed(self):
        parsed = parse_overrides(["capital_growth=0.01", "speed.services=0.4",
                                  "energy_efficiency_gain.energy=0.5"])
        self.assertAlmostEqual(parsed["capital_growth"], 0.01)
        self.assertAlmostEqual(parsed["speed.services"], 0.4)
        self.assertAlmostEqual(parsed["energy_efficiency_gain.energy"], 0.5)
        bad = ["gamma=1", "foo.services=1", "speed.nope=0.1", "speed.services", "speed.services=",
               "speed.services=abc", "ceiling.services=1.5", "start.services=0",
               "capital_share.services=1.2", "output_weight.services=0"]
        for item in bad:
            with self.assertRaises(TechnologyError, msg=item):
                parse_overrides([item])
        with self.assertRaises(TechnologyError):
            parse_overrides(["speed.services=nan"])
        # a direct (non-CLI) override must fail closed too, with the module's own error type
        with self.assertRaises(TechnologyError):
            build_report(["baseline"], None, 5, {"gamma": 1.0})
        with self.assertRaises(TechnologyError):
            build_report(["baseline"], None, 5, {"ceiling.Services": 0.5})


# ---------------------------------------------------------------------------------------
# 6. Demo
# ---------------------------------------------------------------------------------------
class DemoTests(unittest.TestCase):
    def test_demo_compares_baseline_and_acceleration(self):
        document = demo_report()
        self.assertEqual(document["demo"]["horizon_years"], DEMO_HORIZON_YEARS)
        self.assertEqual([item["scenario_id"] for item in document["scenarios"]],
                         ["baseline", "ai_acceleration"])
        self.assertEqual(document["status"], STATUS)
        self.assertIs(document["admission"], False)
        self.assertEqual(document["digest"], canonical_digest(document))
        base = document["scenarios"][0]["aggregate"]["horizon"]
        fast = document["scenarios"][1]["aggregate"]["horizon"]
        self.assertGreater(fast["output_gain_pct"], base["output_gain_pct"])

    def test_demo_horizon_is_configurable_and_deterministic(self):
        first = demo_report(6)
        second = demo_report(6)
        self.assertEqual(first["demo"]["horizon_years"], 6)
        self.assertEqual(first["digest"], second["digest"])

    def test_demo_writes_files_with_matching_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = technology_scenarios.main(["--demo", "--out", folder])
            self.assertEqual(code, 0)
            demo_json = Path(folder) / "demo" / "demo.json"
            demo_md = Path(folder) / "demo" / "demo.md"
            self.assertTrue(demo_json.exists())
            self.assertTrue(demo_md.exists())
            document = json.loads(demo_json.read_text(encoding="utf-8"))
            self.assertEqual(document["digest"], canonical_digest(document))
            self.assertIn(hashlib.sha256(demo_json.read_bytes()).hexdigest()[:16],
                          buffer.getvalue())
            self.assertIn("baseline", buffer.getvalue())
            self.assertIn("ai_acceleration", buffer.getvalue())
            self.assertIn("не прогноз", buffer.getvalue())

    def test_demo_json_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = technology_scenarios.main(["--demo", "--json", "--out", folder])
            self.assertEqual(code, 0)
            document = json.loads(buffer.getvalue())
            self.assertEqual(document["demo"]["horizon_years"], DEMO_HORIZON_YEARS)
            self.assertEqual(list(Path(folder).iterdir()), [])


# ---------------------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as folder:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = technology_scenarios.main(["--out", folder, "--horizon", "8"])
            self.assertEqual(code, 0)
            report_json = Path(folder) / "report.json"
            report_md = Path(folder) / "report.md"
            self.assertTrue(report_json.exists())
            self.assertTrue(report_md.exists())
            document = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], technology.SCHEMA_REPORT)
            self.assertEqual(document["horizon_years"], 8)
            self.assertEqual(document["digest"], canonical_digest(document))
            text = report_md.read_text(encoding="utf-8")
            self.assertIn("RESEARCH_ONLY", text)
            self.assertIn("admission = false", text)
            self.assertIn(document["digest"], text)
            self.assertIn("Growth accounting", text)
            self.assertIn("Ограничения", text)

    def test_cli_json_prints_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = technology_scenarios.main(["--json", "--out", folder])
            self.assertEqual(code, 0)
            document = json.loads(buffer.getvalue())
            self.assertEqual(document["status"], STATUS)
            self.assertEqual(len(document["scenarios"]), 4)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cli_filters_scenario_sector_and_horizon(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = technology_scenarios.main(
                ["--json", "--scenario", "ai_acceleration", "--sector", "services",
                 "--horizon", "5"])
        self.assertEqual(code, 0)
        document = json.loads(buffer.getvalue())
        self.assertEqual(document["scenario_ids"], ["ai_acceleration"])
        self.assertEqual(document["sectors"], ["services"])
        self.assertEqual(document["horizon_years"], 5)
        self.assertEqual(list(document["scenarios"][0]["sectors"]), ["services"])

    def test_cli_failures_exit_nonzero_and_write_nothing(self):
        cases = [
            (["--scenario", "nope"], "unknown scenario"),
            (["--sector", "nope"], "unknown sector"),
            (["--params", "gamma=1"], "unknown parameter"),
            (["--params", "speed.services"], "key=value"),
            (["--horizon", "0"], "horizon"),
            (["--horizon", "51"], "horizon"),
        ]
        for arguments, token in cases:
            with tempfile.TemporaryDirectory() as folder:
                stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = technology_scenarios.main(arguments + ["--out", folder])
                self.assertEqual(code, 1, arguments)
                self.assertIn(token, stderr.getvalue())
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cli_quiet_prints_one_line(self):
        with tempfile.TemporaryDirectory() as folder:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = technology_scenarios.main(["--quiet", "--out", folder])
            self.assertEqual(code, 0)
            lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn(STATUS, lines[0])
            self.assertIn("report.md", lines[0])

    def test_cli_list_commands(self):
        for flag, expected in (("--list-scenarios", SCENARIO_IDS),
                               ("--list-sectors", SECTORS),
                               ("--list-parameters", ("capital_growth", "speed.<sector>"))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(technology_scenarios.main([flag]), 0)
            text = buffer.getvalue()
            for token in expected:
                self.assertIn(token, text)

    def test_cli_reports_io_error_with_exit_two(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "blocked"
            blocker.write_text("not a directory", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = technology_scenarios.main(["--out", str(blocker)])
            self.assertEqual(code, 2)
            self.assertIn("I/O error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
