"""Tests of the external forecasts registry v2 (protocols/external_forecasts_v2.json, docs/201).

The suite is offline and fail-closed.  It checks four properties that make a 26-source catalogue
honest rather than decorative:

* the catalogue really covers the sources docs/173 names (SPF, GDPNow, NY Fed, Cleveland, FOMC SEP,
  IMF/OECD/World Bank/EIU/CBO/NABE, Consensus/Blue Chip, Reuters/WSJ, Michigan, Conference Board,
  Dallas/Kansas City), with a status, a licence and a publication/vintage policy on every entry;
* the validator refuses fabricated values, duplicate ids, unknown statuses/licences and
  status/licence/mapping inconsistencies instead of warning about them;
* coverage arithmetic is exact and the comparison with the current scorecard (two scored sources)
  does not invent a third one;
* the CLI is deterministic, writes JSON/Markdown only where asked and exits 2 on a broken registry.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import external_scorecard_v2 as v2  # noqa: E402


def load_registry() -> dict:
    return json.loads(v2.REGISTRY_PATH.read_text(encoding="utf-8"))


def source_of(registry: dict, source_id: str) -> dict:
    for source in registry["sources"]:
        if source.get("id") == source_id:
            return source
    raise KeyError(source_id)


def error_codes(result: dict) -> list[str]:
    return [error.split(":", 1)[0] for error in result["errors"]]


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# --------------------------------------------------------------------------------------------
# The shipped catalogue
# --------------------------------------------------------------------------------------------
class RegistryCatalogueTests(TempDirMixin):
    def test_registry_has_at_least_twenty_unique_sources(self):
        registry = load_registry()
        ids = [source["id"] for source in registry["sources"]]
        self.assertGreaterEqual(len(ids), v2.MIN_SOURCES)
        self.assertEqual(len(ids), len(set(ids)))
        for source_id in ids:
            self.assertRegex(source_id, r"^[A-Z][A-Z0-9_]+$")

    def test_registry_declares_its_schema_and_the_status_vocabulary(self):
        registry = load_registry()
        self.assertEqual(registry["schema"], v2.REGISTRY_SCHEMA)
        self.assertEqual(registry["registry_id"], v2.REGISTRY_ID)
        self.assertEqual(registry["date"], "2026-11-26")
        for status in v2.STATUSES:
            self.assertTrue(registry["statuses"].get(status), status)
        for licence in v2.LICENCE_STATUSES:
            self.assertTrue(registry["licence_statuses"].get(licence), licence)

    def test_sources_named_by_the_idea_are_present(self):
        ids = {source["id"] for source in load_registry()["sources"]}
        required = {
            "SPF_GDP_MEDIAN",
            "NY_FED_STAFF_NOWCAST",
            "CLEVELAND_FED_NOWCAST",
            "ATLANTA_FED_GDPNOW",
            "BLUE_CHIP_CONSENSUS",
            "CONSENSUS_ECONOMICS",
            "REUTERS_POLL",
            "WSJ_SURVEY",
            "FOMC_SEP",
            "IMF_WEO",
            "OECD_EO",
            "WORLD_BANK_GEP",
            "EIU_FORECAST",
            "CBO_OUTLOOK",
            "NABE_OUTLOOK",
            "UMICH_SURVEY_CONSUMERS",
            "CONFERENCE_BOARD_CCI",
            "DALLAS_FED_TMOS",
            "KANSAS_CITY_FED_MFG",
        }
        self.assertTrue(required <= ids, sorted(required - ids))

    def test_every_status_and_the_main_licence_kinds_are_exercised(self):
        registry = load_registry()
        statuses = {source["status"] for source in registry["sources"]}
        self.assertEqual(statuses, set(v2.STATUSES))
        licences = {source["licence"]["status"] for source in registry["sources"]}
        self.assertTrue({"OPEN", "OPEN_ATTRIBUTION", "RESTRICTED"} <= licences)
        self.assertTrue(licences <= set(v2.LICENCE_STATUSES))

    def test_every_source_has_a_publication_and_vintage_policy(self):
        for source in load_registry()["sources"]:
            publication = source["publication"]
            for field in ("cadence", "vintage_policy", "publication_date_source"):
                self.assertTrue(publication[field].strip(), f"{source['id']}.{field}")
            self.assertTrue(source["horizon"]["native"].strip(), source["id"])
            self.assertTrue(source["licence"]["note"].strip(), source["id"])
            self.assertTrue(source["notes"].strip(), source["id"])
            self.assertTrue(source["url"].startswith("https://"), source["id"])

    def test_registry_contains_no_raw_values(self):
        """The registry is metadata only: no value-like key and no numeric series anywhere."""
        registry = load_registry()

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(key.lower(), v2.RAW_VALUE_KEYS, f"{path}.{key}")
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                numbers = [item for item in node if isinstance(item, (int, float)) and not isinstance(item, bool)]
                if len(node) > 4:
                    self.assertNotEqual(len(numbers), len(node), f"{path}: numeric series of {len(node)}")
                for index, item in enumerate(node):
                    walk(item, f"{path}[{index}]")

        walk(registry, "registry")

    def test_unrate_target_is_documented_as_absent_from_the_protocol(self):
        registry = load_registry()
        self.assertIn("no protocol target in v5", registry["targets"]["UNRATE"])
        self.assertIn("UNRATE", {source["target"] for source in registry["sources"]})


# --------------------------------------------------------------------------------------------
# Validation of the shipped registry
# --------------------------------------------------------------------------------------------
class RegistryValidationTests(TempDirMixin):
    def test_shipped_registry_validates_without_errors_or_warnings(self):
        result = v2.validate_registry(
            load_registry(), register=v2.load_licence_register(), scorecard=v2.load_scorecard()
        )
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["counts"]["sources"], len(load_registry()["sources"]))

    def test_every_status_reason_comes_from_the_closed_set(self):
        for source in load_registry()["sources"]:
            allowed = v2.STATUS_REASONS[source["status"]]
            self.assertIn(source["status_reason"], allowed, source["id"])

    def test_adapter_ready_sources_are_exactly_the_scored_ones(self):
        report = v2.build_report()
        ready = set(report["coverage"]["adapter_ready"]["sources"])
        scored = set(report["coverage"]["scorecard_comparison"]["scored_sources"])
        self.assertEqual(ready, scored)
        self.assertEqual(ready, {"LIVINGSTON_SURVEY", "NY_FED_STAFF_NOWCAST", "SPF_GDP_MEDIAN"})
        self.assertEqual(report["coverage"]["scorecard_comparison"]["mismatches"], [])

    def test_licence_register_references_exist_and_are_not_more_permissive(self):
        registry = load_registry()
        register = v2.load_licence_register()
        for source in registry["sources"]:
            entry_id = source["licence"]["entry"]
            if not entry_id:
                continue
            entry = v2.register_entry(register, entry_id)
            self.assertIsNotNone(entry, source["id"])
            self.assertLessEqual(
                v2._licence_rank(source["licence"]["status"]),
                v2._licence_rank(str(entry["status"]).upper()),
                source["id"],
            )


# --------------------------------------------------------------------------------------------
# Fail-closed validator
# --------------------------------------------------------------------------------------------
class FailClosedTests(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.registry = load_registry()

    def validate(self):
        return v2.validate_registry(
            self.registry, register=v2.load_licence_register(), scorecard=v2.load_scorecard()
        )

    def test_duplicate_id_is_rejected(self):
        source_of(self.registry, "SPF_CPI_1YR")["id"] = "SPF_GDP_MEDIAN"
        result = self.validate()
        self.assertFalse(result["ok"])
        self.assertIn("DUPLICATE_ID", error_codes(result))

    def test_unknown_status_is_rejected(self):
        source_of(self.registry, "IMF_WEO")["status"] = "PROBABLY_FINE"
        result = self.validate()
        self.assertIn("UNKNOWN_STATUS", error_codes(result))
        self.assertFalse(result["ok"])

    def test_unknown_licence_is_rejected(self):
        source_of(self.registry, "IMF_WEO")["licence"]["status"] = "FREE_STUFF"
        result = self.validate()
        self.assertIn("UNKNOWN_LICENCE", error_codes(result))

    def test_restricted_status_cannot_claim_an_open_licence(self):
        source_of(self.registry, "ATLANTA_FED_GDPNOW")["licence"]["status"] = "OPEN"
        result = self.validate()
        self.assertIn("LICENCE_VS_STATUS", error_codes(result))
        self.assertIn("LICENCE_MORE_PERMISSIVE_THAN_REGISTER", error_codes(result))

    def test_restricted_licence_cannot_be_reported_as_adapter_ready(self):
        source = source_of(self.registry, "SPF_GDP_MEDIAN")
        source["licence"]["status"] = "RESTRICTED"
        result = self.validate()
        self.assertIn("STATUS_VS_LICENCE", error_codes(result))

    def test_adapter_ready_requires_a_local_data_path(self):
        source_of(self.registry, "SPF_GDP_MEDIAN")["data_path"] = None
        result = self.validate()
        self.assertIn("READY_WITHOUT_DATA_PATH", error_codes(result))

    def test_adapter_ready_requires_an_exact_mapping(self):
        source = source_of(self.registry, "SPF_GDP_MEDIAN")
        source["horizon"]["mapping"] = "NONE"
        source["horizon"]["protocol_h"] = []
        result = self.validate()
        self.assertIn("READY_WITHOUT_EXACT_MAPPING", error_codes(result))

    def test_adapter_ready_requires_a_licence_register_entry(self):
        source_of(self.registry, "SPF_GDP_MEDIAN")["licence"]["entry"] = None
        result = self.validate()
        self.assertIn("READY_WITHOUT_LICENCE_ENTRY", error_codes(result))

    def test_implemented_adapter_must_be_in_the_adapter_catalogue(self):
        source_of(self.registry, "SPF_GDP_MEDIAN")["adapter"] = "my_magic_adapter"
        result = self.validate()
        self.assertIn("ADAPTER_NOT_IMPLEMENTED", error_codes(result))

    def test_unavailable_reason_must_match_the_mapping(self):
        source = source_of(self.registry, "CLEVELAND_FED_NOWCAST")
        source["horizon"]["mapping"] = "EXACT"
        source["horizon"]["protocol_h"] = [1]
        result = self.validate()
        self.assertIn("UNAVAILABLE_REASON_VS_MAPPING", error_codes(result))

    def test_not_fetched_requires_an_open_licence(self):
        source_of(self.registry, "BOE_MPR")["licence"]["status"] = "RESTRICTED"
        result = self.validate()
        self.assertIn("NOT_FETCHED_WITHOUT_OPEN_LICENCE", error_codes(result))

    def test_not_fetched_requires_a_planned_adapter(self):
        source_of(self.registry, "BOE_MPR")["adapter_state"] = "NOT_APPLICABLE"
        result = self.validate()
        self.assertIn("NOT_FETCHED_WITHOUT_PLANNED_ADAPTER", error_codes(result))

    def test_ecb_spf_is_not_scorable_because_the_protocol_targets_are_us(self):
        registry = load_registry()
        source = source_of(registry, "ECB_SPF")
        self.assertEqual(source["status"], "UNAVAILABLE")
        self.assertEqual(source["status_reason"], "NO_PROTOCOL_TARGET_MAPPING")
        self.assertEqual(source["horizon"]["mapping"], "NONE")
        self.assertIsNone(source["adapter"])
        self.assertEqual(source["adapter_state"], "NOT_APPLICABLE")
        self.assertIsNone(source["data_path"])
        self.assertIn("euro area", source["notes"].lower())
        self.assertIn("2026-11-26", source["notes"])
        result = v2.validate_registry(registry, register=v2.load_licence_register(),
                                      scorecard=v2.load_scorecard())
        self.assertTrue(result["ok"], result["errors"])

    def test_imf_weo_is_not_scorable_on_the_quarterly_protocol_grid(self):
        registry = load_registry()
        source = source_of(registry, "IMF_WEO")
        self.assertEqual(source["status"], "UNAVAILABLE")
        self.assertEqual(source["status_reason"], "NO_PROTOCOL_HORIZON_MAPPING")
        self.assertEqual(source["horizon"]["mapping"], "NONE")
        self.assertIsNone(source["adapter"])
        self.assertEqual(source["adapter_state"], "NOT_APPLICABLE")
        self.assertIn("annual", source["notes"].lower())
        self.assertIn("2026-11-26", source["notes"])

    def test_third_source_attempt_is_documented(self):
        doc = ROOT / "docs" / "174_Внешние_прогнозы_scorecard.md"
        text = doc.read_text(encoding="utf-8")
        self.assertIn("## 13. Попытка третьего scorable", text)
        self.assertIn("NO_PROTOCOL_TARGET_MAPPING", text)
        self.assertIn("NO_PROTOCOL_HORIZON_MAPPING", text)
        self.assertIn("281 PIT", text)

    def test_raw_values_are_rejected(self):
        source_of(self.registry, "IMF_WEO")["values"] = {"2026": 3.1}
        result = self.validate()
        self.assertIn("RAW_VALUES_NOT_ALLOWED", error_codes(result))

    def test_a_numeric_series_is_rejected_even_under_an_innocent_key(self):
        source_of(self.registry, "IMF_WEO")["estimated_series"] = [2.1, 2.2, 2.3, 2.4, 2.5]
        result = self.validate()
        self.assertIn("RAW_VALUES_NOT_ALLOWED", error_codes(result))

    def test_missing_publication_policy_is_rejected(self):
        del source_of(self.registry, "IMF_WEO")["publication"]["vintage_policy"]
        result = self.validate()
        self.assertIn("BAD_TYPE", error_codes(result))

    def test_missing_scorecard_is_a_warning_not_a_failure(self):
        result = v2.validate_registry(load_registry(), register=v2.load_licence_register(), scorecard=None)
        self.assertTrue(result["ok"])
        self.assertTrue(any(w.startswith("SCORECARD_MISSING") for w in result["warnings"]))

    def test_a_custom_scorecard_path_is_used_in_the_comparison(self):
        path = self.tmp / "scorecard.json"
        path.write_text(
            json.dumps({"status": "PARTIAL", "version": "0.1.0", "sources": {},
                        "cross_source": {"scored_sources": [], "scored_points_total": 0}}),
            encoding="utf-8",
        )
        report = v2.build_report(scorecard_path=path)
        comparison = report["coverage"]["scorecard_comparison"]
        self.assertTrue(comparison["file"].endswith("/scorecard.json"), comparison["file"])
        self.assertEqual(comparison["scored_sources"], [])
        self.assertEqual(comparison["ready_but_not_scored"],
                         ["LIVINGSTON_SURVEY", "NY_FED_STAFF_NOWCAST", "SPF_GDP_MEDIAN"])
        self.assertEqual(len(comparison["registry_only"]), len(load_registry()["sources"]))


# --------------------------------------------------------------------------------------------
# Coverage arithmetic
# --------------------------------------------------------------------------------------------
class CoverageTests(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.report = v2.build_report()
        self.cov = self.report["coverage"]
        self.total = self.cov["sources_total"]

    def test_counts_add_up_to_the_catalogue(self):
        for dimension in ("by_status", "by_target", "by_licence", "by_mapping", "by_frequency"):
            block = self.cov[dimension]
            self.assertEqual(sum(item["count"] for item in block.values()), self.total, dimension)
            self.assertAlmostEqual(sum(item["share"] for item in block.values()), 1.0, places=3)

    def test_status_coverage_matches_the_catalogue(self):
        counts = {source["status"]: 0 for source in load_registry()["sources"]}
        for source in load_registry()["sources"]:
            counts[source["status"]] += 1
        for status, count in counts.items():
            self.assertEqual(self.cov["by_status"][status]["count"], count, status)

    def test_unavailable_sources_are_grouped_by_reason(self):
        by_reason = self.cov["unavailable"]["by_reason"]
        self.assertTrue(set(by_reason) <= set(v2.STATUS_REASONS["UNAVAILABLE"]))
        self.assertEqual(sum(len(ids) for ids in by_reason.values()), self.cov["unavailable"]["count"])
        for reason, ids in by_reason.items():
            self.assertTrue(ids, reason)

    def test_ready_target_coverage_is_reported_honestly(self):
        ready = self.cov["adapter_ready"]["targets_with_ready_source"]
        self.assertEqual(ready, {"CPI": False, "GDP": True, "UNRATE": False, "RECESSION": False})

    def test_coverage_output_contains_no_values(self):
        blob = json.dumps(self.report, ensure_ascii=False)

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(key.lower(), v2.RAW_VALUE_KEYS, f"{path}.{key}")
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                numbers = [item for item in node if isinstance(item, (int, float)) and not isinstance(item, bool)]
                if len(node) > 4:
                    self.assertNotEqual(len(numbers), len(node), f"{path}: numeric series")
                for index, item in enumerate(node):
                    walk(item, f"{path}[{index}]")

        walk(self.report, "report")
        self.assertIsInstance(blob, str)

    def test_status_and_reasons_are_explained(self):
        self.assertEqual(self.report["status"], "OK")
        self.assertEqual(self.report["status_reasons"], [])
        self.assertTrue(self.report["gaps"])
        self.assertTrue(any("RESTRICTED" in gap for gap in self.report["gaps"]))


# --------------------------------------------------------------------------------------------
# Determinism and digest
# --------------------------------------------------------------------------------------------
class DeterminismTests(TempDirMixin):
    def test_two_runs_are_byte_identical(self):
        first = json.dumps(v2.build_report(), ensure_ascii=False, indent=1)
        second = json.dumps(v2.build_report(), ensure_ascii=False, indent=1)
        self.assertEqual(first, second)

    def test_content_digest_matches_and_changes_with_the_content(self):
        report = v2.build_report()
        self.assertEqual(v2.content_digest(report), report["content_sha256"])
        report["coverage"]["sources_total"] = 999
        self.assertNotEqual(v2.content_digest(report), report["content_sha256"])

    def test_registry_sha256_matches_the_file(self):
        report = v2.build_report()
        digest = v2.sha256_file(v2.REGISTRY_PATH)
        self.assertEqual(report["registry"]["sha256"], digest)
        self.assertEqual(len(digest), 64)

    def test_tool_reads_no_clock_and_no_network(self):
        source = Path(v2.__file__).read_text(encoding="utf-8")
        for banned in ("import socket", "import urllib", "import requests", "http.client",
                       "urlopen", "datetime.now", "time.time"):
            self.assertNotIn(banned, source)


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
class CliTests(TempDirMixin):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = v2.main(argv)
        return code, out.getvalue(), err.getvalue()

    def broken_registry(self, mutate) -> Path:
        registry = load_registry()
        mutate(registry)
        path = self.tmp / "broken.json"
        path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
        return path

    def test_check_returns_zero_for_a_valid_registry(self):
        code, out, _ = self.run_cli(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("27 sources", out)
        self.assertIn("status: OK", out)
        self.assertIn("ADAPTER_READY 3", out)

    def test_strict_keeps_exit_zero_when_all_criteria_pass(self):
        code, _, _ = self.run_cli(["--check", "--strict"])
        self.assertEqual(code, 0)

    def test_json_flag_prints_the_report(self):
        code, out, _ = self.run_cli(["--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["report_id"], v2.REPORT_ID)
        self.assertTrue(report["validation"]["ok"])
        self.assertEqual(report["coverage"]["sources_total"], len(load_registry()["sources"]))

    def test_broken_registry_exits_two_and_writes_no_file(self):
        path = self.broken_registry(
            lambda registry: source_of(registry, "SPF_CPI_1YR").update({"id": "SPF_GDP_MEDIAN"})
        )
        target = self.tmp / "should_not_exist.json"
        code, out, err = self.run_cli(["--check", "--registry", str(path), "--out", str(target)])
        self.assertEqual(code, 2)
        self.assertIn("DUPLICATE_ID", err)
        self.assertFalse(target.exists())
        self.assertIn("status: INVALID", out)

    def test_missing_registry_exits_two_without_a_traceback(self):
        code, _, err = self.run_cli(["--check", "--registry", str(self.tmp / "nope.json")])
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_out_writes_json_and_markdown_by_suffix(self):
        json_path = self.tmp / "reports" / "coverage.json"
        md_path = self.tmp / "reports" / "coverage.md"
        code, _, _ = self.run_cli(["--out", str(json_path)])
        self.assertEqual(code, 0)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(report["report_id"], v2.REPORT_ID)
        code, _, _ = self.run_cli(["--out", str(md_path)])
        self.assertEqual(code, 0)
        text = md_path.read_text(encoding="utf-8")
        self.assertIn("# External forecasts registry v2 - coverage", text)
        self.assertIn("| ADAPTER_READY | 3 |", text)

    def test_unknown_out_suffix_exits_two(self):
        code, _, err = self.run_cli(["--out", str(self.tmp / "coverage.txt")])
        self.assertEqual(code, 2)
        self.assertIn(".json or .md", err)

    def test_markdown_export_is_deterministic(self):
        first = v2.render_markdown(v2.build_report())
        second = v2.render_markdown(v2.build_report())
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
