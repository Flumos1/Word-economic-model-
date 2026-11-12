"""Official SPF mapping/availability tests (metadata-only, no third-party values committed)."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import spf_mapping as spf  # noqa: E402

VALUES = ROOT / "data" / "raw" / "spf" / "spf_median.json"
METADATA = ROOT / "protocols" / "spf_metadata.json"


class MappingTests(unittest.TestCase):
    def test_official_t2_mapping(self):
        self.assertEqual(
            spf.mapping_table()["T2_GDP_GROWTH"],
            {"0": "drgdp2", "1": "drgdp3", "2": "drgdp4", "4": "drgdp6"},
        )

    def test_growth_code_offsets(self):
        self.assertEqual(
            {code: spf.SPF_GDP_OFFSET[code] for code in spf.SPF_GDP_CODES},
            {"drgdp2": 0, "drgdp3": 1, "drgdp4": 2, "drgdp5": 3, "drgdp6": 4},
        )
        self.assertEqual(spf.PROTOCOL_T2_CODE[0], "drgdp2")
        self.assertEqual(spf.PROTOCOL_T2_CODE[4], "drgdp6")

    def test_drgdp1_is_not_a_forecast_column(self):
        self.assertNotIn("drgdp1", spf.SPF_ALL_CODES)
        self.assertIn("drgdp1", spf.NOT_A_FORECAST_CODE)
        self.assertIn("historical level", spf.NOT_A_FORECAST_CODE["drgdp1"])
        self.assertIn("drgdp1", spf.mapping_table()["not_a_forecast_code"])

    def test_drgdp5_is_off_the_protocol_grid(self):
        self.assertNotIn(3, spf.PROTOCOL_T2_CODE)
        self.assertEqual(spf.SPF_CODE_TO_PROTOCOL_HORIZON["drgdp5"], 3)
        record = spf.forecast_at("T2_GDP_GROWTH", "2020-02-14", 3)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "NO_SPF_HORIZON_CODE_FOR_H3")

    def test_t1_has_no_spf_monthly_yoy_mapping(self):
        record = spf.forecast_at("T1_CPI_YOY", "2020-02-14", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "NO_MONTHLY_YOY_CPI_FORECAST")
        self.assertIsNone(record["code"])
        self.assertIsNone(spf.mapping_table()["inflation_codes"]["INFCPI1YR"]["mapped_to_protocol_target"])

    def test_t3_has_no_nber_recession_probability(self):
        record = spf.forecast_at("T3_RECESSION", "2020-02-14", 3)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "NO_NBER_RECESSION_PROBABILITY_AND_EVENT_MISMATCH")

    def test_inf_cpi_1yr_definition(self):
        definition = spf.VARIABLE_DEFINITIONS["INFCPI1YR"]["official_definition"]
        self.assertIn("four quarters following the survey quarter", definition)
        self.assertIn("geometric average", definition)
        self.assertIn("annualized", definition)

    def test_inf_cpi_10yr_definition_and_limitation(self):
        definition = spf.VARIABLE_DEFINITIONS["INFCPI10YR"]["official_definition"]
        self.assertIn("next 10 years", definition)
        self.assertIn("does not spell out", definition)
        self.assertEqual(spf.mapping_table()["inflation_codes"]["INFCPI10YR"]["mapped_to_protocol_target"], None)

    def test_quote_constants_refer_to_official_documents(self):
        self.assertIn("spf-documentation.pdf", spf.SPF_DOCUMENTATION_URL)
        self.assertIn("spf-release-dates.txt", spf.SPF_RELEASE_DATES_URL)
        self.assertEqual(spf.SPF_PDF_LAST_UPDATE, "July 28, 2026")
        self.assertEqual(spf.SPF_ACCESSED, "2026-11-12")


class DeadlineAvailabilityTests(unittest.TestCase):
    def test_exact_official_deadline_and_release_anchors(self):
        self.assertEqual(spf.deadline_date("2020Q1"), spf.date(2020, 2, 11))
        self.assertEqual(spf.release_date("2020Q1"), spf.date(2020, 2, 14))
        self.assertEqual(spf.release_date("1990Q3"), spf.date(1990, 8, 31))
        self.assertEqual(spf.release_date("2019Q1"), spf.date(2019, 3, 22))
        self.assertEqual(spf.release_date("2026Q1"), spf.date(2026, 3, 6))

    def test_deadline_is_not_enough_for_a_median(self):
        before_deadline = spf.availability("2020-02-10")
        at_deadline = spf.availability("2020-02-11")
        at_release = spf.availability("2020-02-14")
        self.assertFalse(before_deadline.available)
        self.assertTrue(at_deadline.deadline_reached)
        self.assertFalse(at_deadline.release_reached)
        self.assertFalse(at_deadline.available)
        self.assertTrue(at_release.available)
        self.assertEqual(at_release.reason, "AVAILABLE")

    def test_unknown_quarter_uses_conservative_month_end_fallback(self):
        # 2026Q4 is beyond the embedded official anchors; the fallback is conservative.
        self.assertEqual(spf.deadline_date("2026Q4"), spf.date(2026, 11, 30))
        self.assertEqual(spf.release_date("2026Q4"), spf.date(2026, 11, 30))
        self.assertFalse(spf.availability("2026-11-15", "2026Q4").available)
        self.assertTrue(spf.availability("2026-11-30", "2026Q4").available)

    def test_origin_must_be_a_middle_month(self):
        verdict = spf.availability("2020-01-31")
        self.assertFalse(verdict.available)
        self.assertEqual(verdict.reason, "ORIGIN_NOT_AN_SPF_MIDDLE_MONTH")

    def test_pre_real_time_surveys_are_unavailable(self):
        verdict = spf.availability("1990-05-31", "1990Q2")
        self.assertFalse(verdict.available)
        self.assertEqual(verdict.reason, "SPF_NOT_REAL_TIME_BEFORE_1990Q3")
        self.assertTrue(spf.availability("1990-08-31", "1990Q3").available)


class ParsingAndMetadataTests(unittest.TestCase):
    def test_parse_values_partial_payload(self):
        table = spf.parse_values({"series": [
            {"code": "drgdp2", "survey_quarter": "2020Q1", "value": 1.5},
            {"code": "drgdp3", "survey_quarter": "2020Q1", "value": "1.7"},
            {"code": "unknown", "survey_quarter": "2020Q1", "value": 9},
            {"code": "drgdp4", "survey_quarter": "2020Q1", "value": "NaN"},
        ]})
        self.assertEqual(table["drgdp2"]["2020Q1"], 1.5)
        self.assertEqual(table["drgdp3"]["2020Q1"], 1.7)
        self.assertNotIn("unknown", table)
        self.assertNotIn("drgdp4", table)

    def test_empty_or_malformed_values_are_rejected(self):
        for payload in ({}, {"series": []}, {"series": "not-a-list"}, {"series": [{"code": "bad"}]}):
            with self.assertRaises(spf.SpfValuesUnavailable):
                spf.parse_values(payload)

    def test_duplicate_observation_is_rejected(self):
        payload = {"series": [
            {"code": "drgdp2", "survey_quarter": "2020Q1", "value": 1.0},
            {"code": "drgdp2", "survey_quarter": "2020Q1", "value": 1.1},
        ]}
        with self.assertRaises(spf.SpfValuesUnavailable):
            spf.parse_values(payload)

    def test_missing_values_file_is_an_explicit_skip(self):
        with self.assertRaises(spf.SpfValuesUnavailable) as ctx:
            spf.load_values(ROOT / "data" / "raw" / "spf" / "does-not-exist.json")
        self.assertIn("absent", str(ctx.exception))
        record = spf.forecast_at(
            "T2_GDP_GROWTH", "2020-02-14", 0,
            values_path=ROOT / "data" / "raw" / "spf" / "does-not-exist.json",
        )
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "SPF_VALUES_NOT_DOWNLOADED")
        self.assertIn("absent", record["note"])

    def test_metadata_matches_official_codes_and_has_no_values(self):
        result = spf.metadata_check(METADATA)
        self.assertTrue(result["ok"], result["issues"])
        metadata = json.loads(METADATA.read_text(encoding="utf-8"))
        self.assertFalse(metadata["raw_files_committed"])
        self.assertNotIn("series", metadata)
        self.assertIn("philadelphiafed.org", metadata["source_url"])
        self.assertEqual(metadata["values_location"], "data/raw/spf/spf_median.json")

    def test_committed_metadata_does_not_contain_values(self):
        text = METADATA.read_text(encoding="utf-8")
        parsed = json.loads(text)
        for var in parsed.get("variables", []):
            self.assertNotIn("value", var)
            self.assertNotIn("values", var)

    def test_values_file_is_not_tracked_by_git(self):
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "data/raw/spf"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            self.skipTest("git is unavailable in this environment")
        self.assertEqual(result.stdout.strip(), "", "SPF median values must never be tracked by git")


class LocalValuesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not VALUES.exists():
            raise unittest.SkipTest("local git-ignored SPF values are absent; metadata-only tests still run")
        cls.table = spf.load_values(VALUES)

    def test_local_values_cover_all_official_codes(self):
        self.assertEqual(set(self.table), set(spf.SPF_ALL_CODES))
        self.assertGreaterEqual(len(self.table["drgdp2"]), 140)
        self.assertIn("2020Q1", self.table["drgdp2"])
        self.assertIn("2020Q1", self.table["INFCPI1YR"])

    def test_local_value_matches_tool_forecast(self):
        record = spf.forecast_at("T2_GDP_GROWTH", "2020-02-14", 0)
        self.assertTrue(record["available"])
        self.assertEqual(record["survey_quarter"], "2020Q1")
        self.assertEqual(record["code"], "drgdp2")
        self.assertAlmostEqual(record["value"], self.table["drgdp2"]["2020Q1"])

    def test_t2_horizons_one_two_four(self):
        for horizon, code in ((1, "drgdp3"), (2, "drgdp4"), (4, "drgdp6")):
            record = spf.forecast_at("T2_GDP_GROWTH", "2020-02-14", horizon)
            self.assertTrue(record["available"], record)
            self.assertEqual(record["code"], code)

    def test_forecast_before_release_is_unavailable(self):
        record = spf.forecast_at("T2_GDP_GROWTH", "2020-02-10", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "SPF_RELEASE_NOT_REACHED_AT_ORIGIN")

    def test_check_reports_values_available_but_metadata_only(self):
        result = spf.check(VALUES, METADATA)
        self.assertTrue(result["ok"])
        self.assertTrue(result["values"]["available"])
        self.assertEqual(result["values"]["total_observations"], 1022)
        self.assertNotIn("series", result)


class CliTests(unittest.TestCase):
    def test_check_cli_skips_absent_values_cleanly(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = spf.main(["--check", "--values", str(ROOT / "data" / "raw" / "spf" / "absent.json")])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["values"]["available"])
        self.assertIn("reason", payload["values"])

    def test_cli_forecast_outputs_verdict(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = spf.main(["--origin", "2020-02-14", "--target", "T2_GDP_GROWTH",
                             "--horizon", "0", "--values", str(VALUES)])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["available"])
        self.assertEqual(payload["code"], "drgdp2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
