"""Tests for the geopolitics scenario library (docs/177 §4.2, M16, block 16).

Everything runs on a deterministic constructed table carrying the protocol's own labels (no
third-party data, no network); the real FIGARO run is exercised by the CLI, not by unit tests.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import geopolitics_scenarios as gs  # noqa: E402

PROTOCOL = ROOT / "protocols" / "geopolitics_scenarios.json"
FORBIDDEN_KEYS = {"Z", "x", "y", "va", "Z_sparse", "by_sector", "delta_x", "delta_y",
                  "gross_output_vector", "final_demand_vector", "delta_A"}
MAX_REPORT_BYTES = 400_000


def load_document():
    document, sha = gs.load_protocol(PROTOCOL)
    return document, sha


_FIXTURE = {}


def fixture():
    """Validated protocol + labelled fixture table + one full run (cached for the module)."""
    if not _FIXTURE:
        document, sha = load_document()
        summary = gs.validate_protocol(document, gs.load_source_registry())
        table = gs.synthetic_table_for_protocol(document, summary)
        meta = gs._synthetic_meta(table)
        result = gs.build_reports(table, document, meta, sha, summary=summary)
        _FIXTURE.update({"document": document, "sha": sha, "summary": summary, "table": table,
                         "meta": meta, "result": result})
    return _FIXTURE


def reports_by_id(result):
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


def numeric_leaves(node, path=""):
    numbers = []
    if isinstance(node, dict):
        for key, value in node.items():
            numbers.extend(numeric_leaves(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            numbers.extend(numeric_leaves(value, f"{path}[{index}]"))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        numbers.append((path, node))
    return numbers


class ProtocolFile(unittest.TestCase):
    def test_protocol_matches_acceptance_coverage(self):
        fixture_data = fixture()
        document = fixture_data["document"]
        summary = fixture_data["summary"]
        self.assertEqual(document["schema"], gs.SCHEMA)
        self.assertEqual(document["status"], gs.STATUS)
        self.assertIs(document["causal_claim"], False)
        self.assertIs(document["gating"], False)
        self.assertGreaterEqual(summary["scenario_count"], 12)  # B1
        self.assertGreaterEqual(summary["family_count"], 4)  # B1
        self.assertGreaterEqual(summary["country_count"], 6)  # B1
        self.assertGreaterEqual(summary["industry_count"], 10)  # B1
        families = {scenario["family"] for scenario in summary["scenarios"]}
        for required in ("sanctions", "tariffs", "conflict", "export_restriction"):
            self.assertIn(required, families)
        # no scenario duplicates another by family + actors + channel
        signatures = [(scenario["family"], tuple(scenario["actors"]["from"]),
                       tuple(scenario["actors"]["to"]), scenario["channel"])
                      for scenario in summary["scenarios"]]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertEqual(len({scenario["scenario_id"] for scenario in summary["scenarios"]}),
                         summary["scenario_count"])

    def test_every_scenario_declares_required_fields_and_bounds(self):
        document, _sha = load_document()
        for raw in document["scenarios"]:
            for key in ("family", "actors", "sectors", "channel", "size", "duration", "decay",
                        "direction", "source", "pit_date", "assumptions"):
                self.assertIn(key, raw, msg=f"{raw.get('scenario_id')}: нет поля {key}")
            self.assertGreater(raw["size"], 0.0)
            self.assertLessEqual(raw["size"], gs.SIZE_MAX)
            self.assertIsInstance(raw["duration"], int)
            self.assertGreaterEqual(raw["duration"], 1)
            self.assertIn(raw["direction"], gs.DIRECTIONS)
            self.assertIn(raw["channel"], gs.CHANNELS)
            self.assertTrue(raw["actors"]["from"] and raw["actors"]["to"])
            self.assertTrue(raw["sectors"])
            self.assertTrue(raw["assumptions"])
            self.assertEqual(raw["source"]["kind"] in ("assumption", "source_id"), True)
            if raw["source"]["kind"] == "assumption":
                self.assertTrue(raw["source"]["replace_with"])

    def test_direction_checks_are_sign_only(self):
        document, _sha = load_document()
        self.assertEqual(len(document["direction_checks"]), 2)
        ids = {scenario["scenario_id"] for scenario in document["scenarios"]}
        for check in document["direction_checks"]:
            self.assertEqual(check["scope"], "sign_only")
            self.assertIn(check["scenario_id"], ids)
            self.assertLessEqual(check["min_matches"], len(check["targets"]))
            self.assertGreaterEqual(check["min_matches"], 2)
            self.assertEqual(len(check["targets"]), 3)
            for target in check["targets"]:
                self.assertIn(target["expected_sign"], (-1, 0, 1))
                self.assertRegex(target["actor"], r"^[A-Z]{3}$")
            self.assertTrue(check["fact"])

    def test_duplicate_ids_and_signature_collisions_are_rejected(self):
        document, _sha = load_document()
        duplicated = copy.deepcopy(document)
        duplicated["scenarios"].append(copy.deepcopy(duplicated["scenarios"][0]))
        with self.assertRaises(gs.ProtocolError):
            gs.validate_protocol(duplicated)
        collision = copy.deepcopy(document)
        clone = copy.deepcopy(collision["scenarios"][0])
        clone["scenario_id"] = "SAN-CLONE-0001"
        clone["title_ru"] = "клон"
        collision["scenarios"].append(clone)
        with self.assertRaises(gs.ProtocolError):
            gs.validate_protocol(collision)
        gating = copy.deepcopy(document)
        gating["gating"] = True
        with self.assertRaises(gs.ProtocolError):
            gs.validate_protocol(gating)
        causal = copy.deepcopy(document)
        causal["causal_claim"] = True
        with self.assertRaises(gs.ProtocolError):
            gs.validate_protocol(causal)

    def test_broken_records_are_rejected(self):
        document, _sha = load_document()
        mutations = [
            ("size", 0.0),
            ("size", 1.5),
            ("duration", 0),
            ("duration", 1.5),
            ("family", "unknown_family"),
            ("channel", "demand_shock"),  # not allowed for a tariffs scenario
            ("decay", "hyperbolic"),
            ("decay", {"kind": "linear", "half_life_months": 2}),
            ("pit_date", "2018-13-40"),
            ("direction", "upside"),
            ("sectors", []),
            ("assumptions", []),
            ("actors", {"from": [], "to": ["USA"]}),
        ]
        for field, value in mutations:
            broken = copy.deepcopy(document)
            broken["scenarios"][0][field] = value
            with self.assertRaises(gs.ProtocolError, msg=f"не отклонено: {field}={value!r}"):
                gs.validate_protocol(broken)
        no_replacement = copy.deepcopy(document)
        no_replacement["scenarios"][0]["source"] = {"kind": "assumption",
                                                    "id": "ASSUMPTION-X"}
        with self.assertRaises(gs.ProtocolError):
            gs.validate_protocol(no_replacement)
        unknown_source = copy.deepcopy(document)
        unknown_source["scenarios"][0]["source"] = {"kind": "source_id", "id": "NOT_A_SOURCE"}
        with self.assertRaises(gs.ProtocolError):
            gs.validate_protocol(unknown_source, gs.load_source_registry())


class Execution(unittest.TestCase):
    def test_all_scenarios_run_with_passing_balances(self):
        result = fixture()["result"]
        index = result["index"]
        self.assertEqual(index["checks"]["scenario_count"], 15)
        self.assertEqual(index["checks"]["family_count"], 7)
        self.assertTrue(index["checks"]["validation_pass"])
        self.assertTrue(index["checks"]["all_balances_pass"])
        self.assertTrue(index["checks"]["all_feasible"])
        self.assertTrue(index["checks"]["all_technology_productive"])
        self.assertTrue(index["checks"]["hawkins_simon_pass"])
        self.assertLess(index["checks"]["max_abs_material_residual"], 1e-6)
        for report in result["reports"]:
            checks = report["checks"]
            self.assertTrue(checks["validation_pass"])
            self.assertTrue(checks["balances_pass"])
            self.assertTrue(checks["scenario_productive"])
            self.assertTrue(checks["feasible"])
            self.assertLess(checks["scenario_material_residual"],
                            checks["balance_tolerance"] * 1e3)
            self.assertAlmostEqual(report["result"]["total_delta_output"],
                                   sum(report["impacts"]["by_country"].values()), places=1)
            self.assertAlmostEqual(report["result"]["total_delta_output"],
                                   sum(report["impacts"]["by_industry"].values()), places=1)

    def test_reports_contain_no_raw_data(self):
        result = fixture()["result"]
        table = fixture()["table"]
        self.assertEqual(forbidden_keys(result["reports"]), [])
        self.assertEqual(forbidden_keys(result["index"]), [])
        payload = gs.render_report_json(result["index"], result["reports"])
        for key in ('"Z"', '"x"', '"y"', '"va"'):
            self.assertNotIn(key, payload)
        for report in result["reports"]:
            self.assertNotIn("by_sector", report["impacts"])
            self.assertEqual(len(report["impacts"]["by_country"]), table.n_countries)
            self.assertEqual(len(report["impacts"]["by_industry"]), table.n_industries)
            self.assertLess(len(json.dumps(report, ensure_ascii=False)), MAX_REPORT_BYTES)

    def test_determinism_is_byte_identical(self):
        data = fixture()
        document = copy.deepcopy(data["document"])
        result = gs.build_reports(data["table"], document, data["meta"], data["sha"],
                                  summary=gs.validate_protocol(data["document"]))
        first = gs.render_report_json(result["index"], result["reports"])
        second = gs.render_report_json(data["result"]["index"], data["result"]["reports"])
        self.assertEqual(first, second)
        self.assertEqual(result["index"]["digest"], data["result"]["index"]["digest"])
        for left, right in zip(result["reports"], data["result"]["reports"]):
            self.assertEqual(left["digest"], right["digest"])
        self.assertEqual(gs.render_index_markdown(result["index"]),
                         gs.render_index_markdown(data["result"]["index"]))
        for left, right in zip(result["reports"], data["result"]["reports"]):
            self.assertEqual(gs.render_scenario_markdown(left),
                             gs.render_scenario_markdown(right))

    def test_digest_tracks_a_scenario_parameter(self):
        data = fixture()
        changed = copy.deepcopy(data["document"])
        baseline = reports_by_id(data["result"])["TAR-USA-CHN-2018"]["digest"]
        for scenario in changed["scenarios"]:
            if scenario["scenario_id"] == "TAR-USA-CHN-2018":
                scenario["size"] = 0.30
        summary = gs.validate_protocol(changed)
        result = gs.build_reports(data["table"], changed, data["meta"], data["sha"],
                                  only=("TAR-USA-CHN-2018",), summary=summary)
        self.assertNotEqual(result["reports"][0]["digest"], baseline)
        self.assertEqual(len(result["reports"]), 1)

    def test_beneficiaries_are_explicit_none_for_a_pure_barrier(self):
        reports = reports_by_id(fixture()["result"])
        report = reports["TAR-USA-CHN-2018"]
        self.assertEqual(report["asymmetry"]["declared_direction"], "damage")
        self.assertEqual(report["asymmetry"]["observed_direction"], "damage")
        self.assertEqual(report["beneficiaries"]["status"], "none")
        self.assertIn("выгод нет", report["beneficiaries"]["note"])
        self.assertEqual(report["beneficiaries"]["countries"], [])
        self.assertEqual(report["asymmetry"]["gain_total"], 0.0)
        self.assertGreater(report["asymmetry"]["loss_total"], 0.0)

    def test_beneficiaries_are_found_for_declared_diversion(self):
        reports = reports_by_id(fixture()["result"])
        report = reports["SAN-IRN-2018-OIL"]
        self.assertEqual(report["beneficiaries"]["status"], "found")
        self.assertGreater(report["beneficiaries"]["counts"]["countries"], 0)
        self.assertTrue(report["beneficiaries"]["cells"])
        self.assertEqual(report["asymmetry"]["observed_direction"], "both")
        self.assertTrue(report["asymmetry"]["consistent"])
        # beneficiaries carry dataset labels (SA = Saudi Arabia, US = United States)
        targets = {entry["name"] for entry in report["beneficiaries"]["countries"]}
        self.assertTrue({"SA", "US"} & targets, msg=f"нет выгодоприобретателей: {targets}")

    def test_demand_diversion_is_zero_sum(self):
        reports = reports_by_id(fixture()["result"])
        for scenario_id in ("SAN-IRN-2018-OIL", "CHK-ORMUZ-2026", "CONF-TAIWAN-STRAIT-2026"):
            report = reports[scenario_id]
            self.assertEqual(report["result"]["delta_final_demand_total"], 0.0)
            transfers = [detail for detail in report["perturbation"]
                         if detail["channel"] == "demand_diversion"]
            self.assertTrue(transfers)
            for detail in transfers:
                self.assertTrue(detail["zero_sum"])

    def test_asymmetry_totals_match_the_marginals(self):
        for report in fixture()["result"]["reports"]:
            asymmetry = report["asymmetry"]
            # gain - loss is the aggregate Δвыпуск: both sides are computed from the same cells
            self.assertAlmostEqual(asymmetry["gain_total"] - asymmetry["loss_total"],
                                   report["result"]["total_delta_output"], delta=0.05)
            by_country = report["impacts"]["by_country"]
            self.assertAlmostEqual(sum(by_country.values()),
                                   report["result"]["total_delta_output"], places=1)
            self.assertAlmostEqual(asymmetry["gain_share_of_abs"],
                                   asymmetry["gain_total"]
                                   / (asymmetry["gain_total"] + asymmetry["loss_total"]),
                                   delta=1e-5)

    def test_decay_profile_is_declared_not_dynamic(self):
        reports = reports_by_id(fixture()["result"])
        tariffs = reports["TAR-USA-CHN-2018"]["decay"]
        self.assertEqual(tariffs["kind"], "none")
        self.assertEqual(len(tariffs["profile"]), tariffs["duration_months"] + 1)
        self.assertEqual(tariffs["profile"][-1]["factor"], 1.0)
        self.assertEqual(tariffs["effective_size_at_horizon"], 0.25)
        linear = reports["SAN-RUS-2022-ENERGY"]["decay"]
        self.assertEqual(linear["kind"], "linear")
        self.assertEqual(linear["profile"][0]["factor"], 1.0)
        self.assertEqual(linear["profile"][-1]["factor"], 0.0)
        self.assertEqual(linear["effective_size_at_horizon"], 0.0)
        for report in reports.values():
            self.assertEqual(report["decay"]["solved_at"], "peak")
            self.assertIn("не динамика", report["decay"]["note"])


class DirectionChecks(unittest.TestCase):
    def test_russia_2022_passes_by_sign_only(self):
        reports = reports_by_id(fixture()["result"])
        check = reports["SAN-RUS-2022-ENERGY"]["direction_check"]
        self.assertEqual(check["scope"], "sign_only")
        self.assertEqual(check["status"], "run")
        self.assertEqual(check["targets_total"], 3)
        self.assertGreaterEqual(check["matched"], 2)
        self.assertTrue(check["pass"])
        self.assertIs(check["magnitude_reported"], False)
        self.assertEqual({target["actor"] for target in check["targets"]},
                         {"RUS", "USA", "DEU"})
        for target in check["targets"]:
            self.assertIn(target["sign"], (-1, 0, 1))
            self.assertEqual(target["match"], target["sign"] == target["expected_sign"])

    def test_iran_2018_passes_on_the_declared_proxy(self):
        reports = reports_by_id(fixture()["result"])
        check = reports["SAN-IRN-2018-OIL"]["direction_check"]
        self.assertTrue(check["pass"])
        iran = next(target for target in check["targets"] if target["actor"] == "IRN")
        self.assertEqual(iran["resolved_via"], "proxy")
        self.assertEqual(iran["table_label"], "ROW")
        self.assertTrue(check["proxy"])
        self.assertIn("прокси", " ".join(check["proxy"]).lower())
        # the scenario report must warn that its numbers are not an estimate of Iran
        report = reports["SAN-IRN-2018-OIL"]
        self.assertTrue(report["proxy"])
        self.assertIn("прокси", report["proxy_warning"].lower())
        self.assertIn("НЕ являются оценкой", report["proxy_warning"])
        entry = next(item for item in fixture()["result"]["index"]["scenarios"]
                     if item["id"] == "SAN-IRN-2018-OIL")
        self.assertTrue(entry["proxy"])

    def test_direction_check_block_has_no_magnitudes(self):
        reports = reports_by_id(fixture()["result"])
        checked = 0
        for report in reports.values():
            block = report.get("direction_check")
            if block is None:
                continue
            checked += 1
            numbers = numeric_leaves(block)
            for path, value in numbers:
                self.assertIsInstance(value, int, msg=f"магнитуда в блоке проверки: {path}={value}")
            payload = json.dumps(block, ensure_ascii=False).lower()
            for token in ("delta_output", "magnitude_value", "delta_x"):
                self.assertNotIn(token, payload)
        self.assertEqual(checked, 2)
        for check in fixture()["result"]["index"]["direction_checks"]:
            for path, value in numeric_leaves(check):
                self.assertIsInstance(value, int, msg=f"магнитуда в индексе: {path}={value}")


class FailClosed(unittest.TestCase):
    def test_missing_protocol_is_blocked_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with self.assertRaises(gs.BlockedInput):
                gs.load_protocol(Path(tmp) / "absent.json")
            code = gs.main(["--protocol", str(Path(tmp) / "absent.json"),
                            "--synthetic", "--out", str(out)])
            self.assertEqual(code, gs.EXIT_BLOCKED)
            self.assertFalse(out.exists())

    def test_invalid_protocol_exits_1_and_writes_nothing(self):
        document, _sha = load_document()
        broken = copy.deepcopy(document)
        broken["scenarios"][0]["size"] = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            out = Path(tmp) / "out"
            code = gs.main(["--protocol", str(path), "--synthetic", "--out", str(out)])
            self.assertEqual(code, gs.EXIT_INVALID)
            self.assertFalse(out.exists())

    def test_unknown_actor_and_sector_fail_closed(self):
        data = fixture()
        unknown_actor = copy.deepcopy(data["document"])
        unknown_actor["scenarios"][0]["actors"]["from"] = ["XXX"]
        with self.assertRaises(gs.ProtocolError):
            gs.build_reports(data["table"], unknown_actor, data["meta"], data["sha"],
                             only=("TAR-USA-CHN-2018",))
        unknown_sector = copy.deepcopy(data["document"])
        unknown_sector["scenarios"][3]["sectors"] = ["CPA_ZZZ"]
        with self.assertRaises(gs.ProtocolError):
            gs.build_reports(data["table"], unknown_sector, data["meta"], data["sha"],
                             only=("TAR-USA-CHN-2018",))
        unknown_only = copy.deepcopy(data["document"])
        with self.assertRaises(gs.ProtocolError):
            gs.build_reports(data["table"], unknown_only, data["meta"], data["sha"],
                             only=("NO-SUCH-SCENARIO",))

    def test_cli_synthetic_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "geo"
            code = gs.main(["--protocol", str(PROTOCOL), "--synthetic", "--out", str(out),
                            "--only", "TAR-USA-CHN-2018,AGR-EU-MERCOSUR-2026"])
            self.assertEqual(code, gs.EXIT_OK)
            report_json = out / "report.json"
            report_md = out / "report.md"
            self.assertTrue(report_json.is_file())
            self.assertTrue(report_md.is_file())
            self.assertTrue((out / "reports" / "TAR-USA-CHN-2018.json").is_file())
            self.assertTrue((out / "reports" / "AGR-EU-MERCOSUR-2026.md").is_file())
            payload = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], gs.STATUS)
            self.assertEqual(len(payload["scenarios"]), 2)
            text = report_md.read_text(encoding="utf-8")
            self.assertIn(gs.STATUS, text)
            self.assertIn("Проверки направления", text)

    def test_gating_and_causal_claim_are_false_everywhere(self):
        result = fixture()["result"]
        self.assertIs(result["index"]["gating"], False)
        self.assertIs(result["index"]["causal_claim"], False)
        self.assertEqual(result["index"]["status"], gs.STATUS)
        self.assertIn("не допуск", result["index"]["status_note"])
        for report in result["reports"]:
            self.assertIs(report["gating"], False)
            self.assertIs(report["causal_claim"], False)
            self.assertIs(report["checks"]["gating"], False)
            self.assertIs(report["checks"]["causal_claim"], False)
            self.assertEqual(report["status"], gs.STATUS)
            self.assertIn("не допуск", report["status_note"])


if __name__ == "__main__":
    unittest.main()
