"""Tests of the external forecast scorecard (tools/external_forecast_scorecard.py, task-54).

Covers the four things that make such a scorecard honest rather than decorative:
point-in-time publication rules, the no-look-ahead gate, the licence gate (values only when the
register allows publication) and the explicit treatment of missing data.  The suite runs offline:
the only local inputs are the git-ignored SPF values and the ALFRED vintage archives, and both are
skipped cleanly when absent (the tool itself also reports them as unavailable, it never invents data).
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import external_forecast_scorecard as sc  # noqa: E402

VALUES = ROOT / "data" / "raw" / "spf" / "spf_median.json"
VINTAGES = ROOT / "data" / "raw" / "GDPC1" / "vintages.zip"
NYFED = ROOT / "data" / "raw" / "ny_fed" / "nowcast.csv"
LOCAL_DATA = VALUES.exists() and VINTAGES.exists()
LOCAL_SCORECARD_DATA = LOCAL_DATA and NYFED.exists()


def quantile_dict(value, levels=sc.PROTOCOL_QUANTILES):
    return {level: float(value) for level in levels}


class FixedTableSpf(sc.SpfAdapter):
    """SPF adapter with an injected table: no dependence on the git-ignored file."""

    def __init__(self, table, **kwargs):
        kwargs.setdefault("table", table)
        super().__init__(**kwargs)


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# --------------------------------------------------------------------------------------------
# Protocol and PIT rules
# --------------------------------------------------------------------------------------------
class PitRuleTests(TempDirMixin):
    def test_protocol_windows_are_the_frozen_ones(self):
        protocol = json.loads(sc.PROTOCOL_PATH.read_text(encoding="utf-8"))
        self.assertEqual(protocol["status"], "FROZEN")
        self.assertEqual(protocol["windows"]["W1"]["start"], "1990-01")
        self.assertEqual(protocol["windows"]["W1"]["end"], "2006-12")
        self.assertEqual(protocol["windows"]["W2"]["start"], "2007-01")
        self.assertEqual(protocol["windows"]["W2"]["end"], "2019-12")
        self.assertEqual(protocol["actuals"]["primary"], "first publication of the target value")

    def test_pit_gate_rejects_future_publication(self):
        ok, why = sc.pit_gate("2019-03-22", "2019-02-28")
        self.assertFalse(ok)
        self.assertEqual(why, "LOOKAHEAD_REJECTED")
        ok, why = sc.pit_gate("2019-02-22", "2019-02-28")
        self.assertTrue(ok)
        self.assertEqual(why, "OK")
        # published on the origin itself: information is available at the origin
        self.assertEqual(sc.pit_gate("2019-02-28", "2019-02-28"), (True, "OK"))
        # an unknown publication date is not usable information
        self.assertEqual(sc.pit_gate(None, "2019-02-28"), (False, "PUBLICATION_DATE_UNKNOWN"))

    def test_actual_gate_requires_a_fact_published_after_the_origin(self):
        actual = {"primary": 2.5, "primary_vintage": "2019-05-01"}
        self.assertEqual(sc.actual_pit_gate(actual, "2019-03-31"), (True, "OK"))
        # the "fact" was already public at the origin -> the point is not a forecast
        known = {"primary": 2.5, "primary_vintage": "2019-03-31"}
        self.assertEqual(sc.actual_pit_gate(known, "2019-03-31"),
                         (False, "ACTUAL_PUBLISHED_AT_OR_ORIGIN"))
        self.assertEqual(sc.actual_pit_gate({"primary": None, "primary_vintage": None}, "2019-03-31"),
                         (False, "ACTUAL_NOT_AVAILABLE"))
        self.assertEqual(sc.actual_pit_gate(None, "2019-03-31"), (False, "ACTUAL_NOT_AVAILABLE"))

    def test_origin_must_be_a_month_end(self):
        adapter = FixedTableSpf({"drgdp2": {"2019Q1": 2.0}}, data_root=self.tmp)
        record = adapter.forecast(sc.TARGET_T2, "2019-02-15", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "ORIGIN_NOT_MONTH_END")

    def test_spf_h0_uses_only_the_middle_month_of_the_survey_quarter(self):
        adapter = FixedTableSpf({"drgdp2": {"2020Q1": 2.0, "2020Q2": 2.2}}, data_root=self.tmp)
        # 2020 is a leap year: the February month end is the 29th, not the 28th
        self.assertEqual(adapter.survey_quarter("2020-02-29", 0), ("2020Q1", "OK"))
        self.assertEqual(adapter.survey_quarter("2020-01-31", 0),
                         (None, "ORIGIN_NOT_A_SURVEY_MONTH"))
        january = adapter.forecast(sc.TARGET_T2, "2020-01-31", 0)
        self.assertEqual(january["reason"], "ORIGIN_NOT_A_SURVEY_MONTH")
        february = adapter.forecast(sc.TARGET_T2, "2020-02-29", 0)
        self.assertTrue(february["available"])
        self.assertEqual(february["forecast"], 2.0)
        self.assertEqual(february["publication_date"], "2020-02-14")  # 2020Q1 news release

    def test_spf_h1_uses_the_origin_quarter_and_its_release(self):
        adapter = FixedTableSpf({"drgdp3": {"2019Q1": 1.4}}, data_root=self.tmp)
        self.assertEqual(adapter.survey_quarter("2019-03-31", 1), ("2019Q1", "OK"))
        record = adapter.forecast(sc.TARGET_T2, "2019-03-31", 1)
        self.assertTrue(record["available"])
        self.assertEqual(record["code"], "drgdp3")
        self.assertEqual(record["survey_quarter"], "2019Q1")
        self.assertLessEqual(record["publication_date"], record["origin"])

    def test_spf_shutdown_quarter_is_not_available_in_february(self):
        # 2019Q1 was released on 2019-03-22 (government shutdown); the February month end is too early
        adapter = FixedTableSpf({"drgdp2": {"2019Q1": 2.0}}, data_root=self.tmp)
        record = adapter.forecast(sc.TARGET_T2, "2019-02-28", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "SPF_RELEASE_AFTER_ORIGIN")
        self.assertEqual(record["publication_date"], "2019-03-22")

    def test_spf_before_1990q3_is_not_real_time(self):
        adapter = FixedTableSpf({"drgdp2": {"1990Q1": 1.3}}, data_root=self.tmp)
        record = adapter.forecast(sc.TARGET_T2, "1990-02-28", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "SPF_NOT_REAL_TIME_BEFORE_1990Q3")

    def test_spf_missing_survey_quarter_is_explicit(self):
        adapter = FixedTableSpf({"drgdp2": {"2019Q1": 2.0}}, data_root=self.tmp)
        record = adapter.forecast(sc.TARGET_T2, "2019-08-31", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "SURVEY_QUARTER_NOT_IN_FILE")

    def test_spf_horizon_outside_the_grid_is_not_silently_mapped(self):
        adapter = FixedTableSpf({"drgdp2": {"2019Q1": 2.0}}, data_root=self.tmp)
        record = adapter.forecast(sc.TARGET_T2, "2019-02-28", 3)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "NO_SPF_HORIZON_CODE_FOR_H3")


# --------------------------------------------------------------------------------------------
# Losses
# --------------------------------------------------------------------------------------------
class LossTests(unittest.TestCase):
    def test_crps_of_a_degenerate_forecast_equals_the_absolute_error(self):
        for actual, point in ((0.0, 1.0), (2.5, 1.0), (-3.0, 1.0), (1.0, 1.0)):
            self.assertAlmostEqual(
                sc.crps_from_quantiles(quantile_dict(point), actual), abs(actual - point), places=12
            )
        self.assertAlmostEqual(sc.crps_from_quantiles(quantile_dict(0.0), 0.0), 0.0, places=12)

    def test_crps_rejects_a_quantile_crossing_and_a_wrong_grid(self):
        crossing = {level: float(i) for i, level in enumerate(sc.PROTOCOL_QUANTILES)}
        crossing[0.95] = -5.0
        with self.assertRaises(ValueError):
            sc.crps_from_quantiles(crossing, 1.0)
        with self.assertRaises(ValueError):
            sc.crps_from_quantiles({0.1: 1.0, 0.9: 2.0}, 1.0)  # not the 19-level protocol grid
        with self.assertRaises(ValueError):
            sc.crps_from_quantiles(quantile_dict(1.0), float("nan"))

    def test_brier_score(self):
        self.assertAlmostEqual(sc.brier_score(0.7, 1.0), 0.09, places=12)
        self.assertAlmostEqual(sc.brier_score(0.7, 0.0), 0.49, places=12)
        with self.assertRaises(ValueError):
            sc.brier_score(1.2, 1.0)
        with self.assertRaises(ValueError):
            sc.brier_score(0.5, 0.5)

    def test_point_only_records_have_no_imputed_distribution_metrics(self):
        records = [
            {"status": "SCORED", "forecast": 2.0, "actual": 2.5},
            {"status": "SCORED", "forecast": 1.0, "actual": 2.5},
        ]
        block = sc.score_records(records)
        self.assertAlmostEqual(block["metrics"]["rmse"], 1.118033988749895, places=10)
        self.assertAlmostEqual(block["metrics"]["mae"], 1.0, places=12)
        self.assertAlmostEqual(block["metrics"]["bias"], -1.0, places=12)
        self.assertIsNone(block["metrics"]["crps"])
        self.assertIsNone(block["metrics"]["brier"])
        self.assertEqual(block["metric_status"]["crps"], "NO_PREDICTIVE_DISTRIBUTION")
        self.assertEqual(block["metric_status"]["brier"], "NO_EVENT_PROBABILITY_MAPPING")

    def test_distribution_and_event_records_are_scored(self):
        records = [
            {"status": "SCORED", "forecast": 2.0, "actual": 2.0, "quantiles": quantile_dict(2.0),
             "probability": 0.8, "outcome": 1.0},
            {"status": "SCORED", "forecast": 4.0, "actual": 2.0, "quantiles": quantile_dict(4.0),
             "probability": 0.2, "outcome": 0.0},
        ]
        block = sc.score_records(records)
        self.assertAlmostEqual(block["metrics"]["crps"], 1.0, places=12)  # mean(0, |2-4|)
        self.assertAlmostEqual(block["metrics"]["coverage_90"], 0.5, places=12)
        self.assertAlmostEqual(block["metrics"]["brier"], 0.04, places=12)  # mean(0.04, 0.04)
        self.assertEqual(block["metric_status"]["crps"], "AVAILABLE")
        self.assertEqual(block["metric_status"]["brier"], "AVAILABLE")
        self.assertEqual(block["distribution_points"], 2)
        self.assertEqual(block["event_points"], 2)

    def test_secondary_actuals_are_reported_separately(self):
        records = [
            {"status": "SCORED", "forecast": 2.0, "actual": 2.0, "secondary_actual": 3.0},
            {"status": "SCORED", "forecast": 2.0, "actual": 2.0, "secondary_actual": 1.0},
        ]
        block = sc.score_records(records)
        self.assertEqual(block["metrics"]["rmse"], 0.0)
        self.assertEqual(block["secondary"]["n"], 2)
        self.assertAlmostEqual(block["secondary"]["rmse"], 1.0, places=12)
        self.assertAlmostEqual(block["secondary"]["rmse_delta_vs_primary"], 1.0, places=12)


# --------------------------------------------------------------------------------------------
# Licence gate
# --------------------------------------------------------------------------------------------
class LicenceTests(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.register = sc.load_licence_register()

    def test_spf_is_open_for_scoring_but_its_values_are_not_publishable(self):
        verdict = sc.licence_verdict(sc.register_entry(self.register, "PHILADELPHIA_FED"))
        self.assertTrue(verdict["usable"])
        self.assertEqual(verdict["usage"], "SCORED")
        self.assertFalse(verdict["values_publishable"])
        self.assertIn("OUTSIDE GIT", verdict["publication_blocker"].upper())

    def test_restricted_entry_is_never_usable(self):
        verdict = sc.licence_verdict(sc.register_entry(self.register, "ATLANTA_FED_GDPNOW"))
        self.assertEqual(verdict["status"], "RESTRICTED")
        self.assertFalse(verdict["usable"])
        self.assertEqual(verdict["usage"], "NOT_USED")
        self.assertEqual(verdict["reason"], "LICENCE_RESTRICTED")

    def test_missing_entry_is_not_usable(self):
        verdict = sc.licence_verdict(None)
        self.assertFalse(verdict["usable"])
        self.assertEqual(verdict["reason"], "NO_LICENCE_REGISTER_ENTRY")

    def test_publication_block_omits_values_unless_allowed(self):
        records = [{"origin": "2019-02-28", "target_period": "2019-03-31", "status": "SCORED",
                    "forecast": 2.0, "actual": 2.4, "publication_date": "2019-02-22"}]
        blocked = sc.publication_block(records, sc.licence_verdict(
            sc.register_entry(self.register, "PHILADELPHIA_FED")))
        self.assertFalse(blocked["values_published"])
        self.assertIsNone(blocked["sample"])
        open_entry = {"id": "SYNTHETIC", "status": "OPEN", "conditions": ["attribution"]}
        allowed = sc.publication_block(records, sc.licence_verdict(open_entry))
        self.assertTrue(allowed["values_published"])
        self.assertEqual(len(allowed["sample"]), 1)
        self.assertEqual(allowed["sample"][0]["forecast"], 2.0)

    def test_project_policy_keeps_values_out_even_when_the_licence_allows_them(self):
        records = [{"origin": "2019-02-28", "target_period": "2019-03-31", "status": "SCORED",
                    "forecast": 2.0, "actual": 2.4, "publication_date": "2019-02-22"}]
        open_entry = {"id": "SYNTHETIC", "status": "OPEN", "conditions": ["attribution"]}
        suppressed = sc.publication_block(records, sc.licence_verdict(open_entry),
                                          allow_values=False)
        self.assertFalse(suppressed["values_published"])
        self.assertEqual(suppressed["reason"], "PROJECT_VALUES_NOT_PUBLISHED")
        self.assertIsNone(suppressed["sample"])
        self.assertNotIn("forecast\"", json.dumps(suppressed))

    def test_gdpnow_values_are_never_read_even_if_a_file_exists(self):
        (self.tmp / "gdpnow").mkdir(parents=True)
        (self.tmp / "gdpnow" / "nowcast.csv").write_text(
            "date,quarter,nowcast,provenance\n2019-02-28,2019Q1,3.0,PUBLISHED\n", encoding="utf-8")
        adapter = sc.RestrictedAdapter(data_root=self.tmp, register=self.register)
        self.assertEqual(adapter.status()["status"], "RESTRICTED_NOT_USED")
        record = adapter.forecast(sc.TARGET_T2, "2019-02-28", 0)
        self.assertFalse(record["available"])
        self.assertEqual(record["reason"], "LICENCE_RESTRICTED")
        self.assertIsNone(record["forecast"])
        self.assertFalse(hasattr(adapter, "rows") and adapter.rows)


# --------------------------------------------------------------------------------------------
# Missing data
# --------------------------------------------------------------------------------------------
class MissingDataTests(TempDirMixin):
    def test_absent_spf_values_are_reported_not_fabricated(self):
        adapter = sc.SpfAdapter(data_root=self.tmp)
        status = adapter.status()
        self.assertEqual(status["status"], "UNAVAILABLE")
        self.assertEqual(status["reason"], "SPF_VALUES_NOT_DOWNLOADED")
        record = adapter.forecast(sc.TARGET_T2, "2019-02-28", 0)
        self.assertFalse(record["available"])
        self.assertIsNone(record["forecast"])

    def test_report_without_any_local_source_is_not_available(self):
        report = sc.build_report(data_root=self.tmp, horizons=(0,), max_origins=3, generated_at="FIXED")
        self.assertEqual(report["status"], "NOT_AVAILABLE")
        self.assertIn("NO_SCORED_POINTS", report["status_reasons"])
        for source_id in ("SPF_GDP_MEDIAN", "NY_FED_STAFF_NOWCAST"):
            self.assertEqual(report["sources"][source_id]["status"]["status"], "UNAVAILABLE")
            self.assertEqual(report["sources"][source_id]["points"]["scored"], 0)
        self.assertEqual(report["cross_source"]["scored_points_total"], 0)

    def test_points_without_first_publication_actuals_are_excluded(self):
        # synthetic SPF values and no ALFRED vintage archive: the fact side is missing
        (self.tmp / "spf").mkdir(parents=True)
        (self.tmp / "spf" / "spf_median.json").write_text(json.dumps({"series": [
            {"code": "drgdp2", "survey_quarter": "1990Q3", "value": 1.0},
            {"code": "drgdp2", "survey_quarter": "1990Q4", "value": 2.0},
        ]}), encoding="utf-8")
        adapter = sc.SpfAdapter(data_root=self.tmp)
        self.assertEqual(adapter.status()["status"], "AVAILABLE")
        protocol = json.loads(sc.PROTOCOL_PATH.read_text(encoding="utf-8"))
        actuals = sc.ActualCache(data_root=self.tmp)
        block = sc._score_window(adapter, protocol, sc.TARGET_T2, 0, "W1",
                                 actuals=actuals, max_origins=12)
        self.assertEqual(block["scored"], 0)
        self.assertGreater(block["exclusions"].get("ACTUAL_NOT_AVAILABLE", 0), 0)
        self.assertEqual(block["metrics"]["rmse"], None)

    def test_unparseable_spf_payload_is_unavailable(self):
        (self.tmp / "spf").mkdir(parents=True)
        (self.tmp / "spf" / "spf_median.json").write_text("{not json", encoding="utf-8")
        adapter = sc.SpfAdapter(data_root=self.tmp)
        self.assertEqual(adapter.status()["reason"], "SPF_VALUES_UNREADABLE")


# --------------------------------------------------------------------------------------------
# NY Fed adapter against a documented fixture
# --------------------------------------------------------------------------------------------
class NyFedAdapterTests(TempDirMixin):
    def _write(self, rows: str) -> None:
        (self.tmp / "ny_fed").mkdir(parents=True, exist_ok=True)
        (self.tmp / "ny_fed" / "nowcast.csv").write_text(
            "date,quarter,nowcast,provenance\n" + rows, encoding="utf-8")

    def test_only_published_rows_before_the_origin_are_used(self):
        self._write(
            "2019-01-31,2019Q1,2.5,PUBLISHED\n"
            "2019-02-28,2019Q1,2.7,PUBLISHED\n"
            "2019-03-29,2019Q1,2.9,RECONSTRUCTED\n"
            "2019-04-30,2019Q1,3.1,PUBLISHED\n"
        )
        adapter = sc.NyFedAdapter(data_root=self.tmp)
        self.assertEqual(adapter.status()["status"], "AVAILABLE")
        february = adapter.forecast(sc.TARGET_T2, "2019-02-28", 0)
        self.assertTrue(february["available"])
        self.assertEqual(february["forecast"], 2.7)  # not the later 3.1
        self.assertEqual(february["publication_date"], "2019-02-28")
        march = adapter.forecast(sc.TARGET_T2, "2019-03-31", 0)
        self.assertFalse(march["available"])
        self.assertEqual(march["reason"], "RECONSTRUCTED_NOT_REAL_TIME")

    def test_unknown_provenance_rows_are_rejected_at_load(self):
        self._write(
            "2019-01-31,2019Q1,2.5,PUBLISHED\n"
            "2019-02-28,2019Q1,2.7,GUESSED\n"
            "not-a-date,2019Q1,2.5,PUBLISHED\n"
        )
        adapter = sc.NyFedAdapter(data_root=self.tmp)
        self.assertEqual(adapter.status()["status"], "AVAILABLE")
        self.assertEqual(adapter.row_errors.get("ROW_PROVENANCE_UNKNOWN"), 1)
        self.assertEqual(adapter.row_errors.get("ROW_UNPARSEABLE"), 1)
        self.assertEqual(len(adapter.rows), 1)
        record = adapter.forecast(sc.TARGET_T2, "2019-02-28", 0)
        self.assertEqual(record["forecast"], 2.5)  # the rejected row is not a candidate

    def test_absent_archive_is_unavailable_with_schema(self):
        adapter = sc.NyFedAdapter(data_root=self.tmp)
        self.assertEqual(adapter.status()["reason"], "ARCHIVE_NOT_LOCAL")
        self.assertIn("provenance", adapter.metadata()["values_file"]["schema"]["columns"])

    @unittest.skipUnless(LOCAL_DATA, "local ALFRED vintages are absent (git-ignored)")
    def test_fixture_is_scored_against_first_publication_actuals(self):
        self._write("2019-01-31,2019Q1,2.5,PUBLISHED\n2019-02-28,2019Q1,2.7,PUBLISHED\n")
        protocol = json.loads(sc.PROTOCOL_PATH.read_text(encoding="utf-8"))
        adapter = sc.NyFedAdapter(data_root=self.tmp)
        actuals = sc.ActualCache(data_root=ROOT / "data" / "raw")
        block = sc._score_window(adapter, protocol, sc.TARGET_T2, 0, "W2",
                                 actuals=actuals, max_origins=156)
        self.assertGreaterEqual(block["scored"], 1)
        self.assertIsNotNone(block["metrics"]["rmse"])
        # the fixture only covers 2019Q1, so the other origins are simply not covered
        self.assertGreater(block["exclusions"].get("NO_PUBLICATION_BEFORE_ORIGIN", 0), 0)
        self.assertEqual(block["exclusions"].get("RECONSTRUCTED_NOT_REAL_TIME", 0), 0)


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(LOCAL_SCORECARD_DATA,
                     "local SPF values, NY Fed archive or ALFRED vintages are absent (git-ignored)")
class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = sc.build_report(horizons=(0,), generated_at="2026-11-18T00:00:00Z")

    def test_report_structure(self):
        report = self.report
        for key in ("report_id", "version", "tool", "purpose", "status", "protocol",
                    "licence_register", "pit_rules", "metric_definitions", "sources",
                    "cross_source", "gaps", "limitations", "content_sha256"):
            self.assertIn(key, report)
        self.assertEqual(report["report_id"], "AMFE-EXTERNAL-FORECAST-SCORECARD")
        self.assertEqual(report["protocol"]["status"], "FROZEN")
        self.assertEqual(report["criteria"]["min_scored_sources"], sc.MIN_SCORED_SOURCES)
        self.assertEqual(report["criteria"]["min_scored_points"], sc.MIN_SCORED_POINTS)
        self.assertEqual(report["criteria"]["m2_min_scored_sources"], sc.M2_MIN_SCORED_SOURCES)
        self.assertEqual(report["publication_policy"]["third_party_values"],
                         sc.REPORT_VALUES_POLICY)
        self.assertEqual(set(report["sources"]), set(sc.SOURCE_SPECS))

    def test_report_has_the_five_required_metrics(self):
        spf = self.report["sources"]["SPF_GDP_MEDIAN"]
        block = spf["targets"][sc.TARGET_T2]["0"]["windows"]["W1"]
        for metric in ("rmse", "mae", "crps", "brier", "coverage_90"):
            self.assertIn(metric, block["metrics"])
            self.assertIn(metric, block["metric_status"])
        self.assertEqual(block["metrics"]["rmse"], self.report["sources"]["SPF_GDP_MEDIAN"]
                         ["targets"][sc.TARGET_T2]["0"]["windows"]["W1"]["metrics"]["rmse"])
        self.assertGreater(block["scored"], 0)
        self.assertIsNotNone(block["coverage_share"])
        self.assertEqual(block["metric_status"]["crps"], "NO_PREDICTIVE_DISTRIBUTION")

    def test_report_contains_no_third_party_forecast_values(self):
        text = json.dumps(self.report, ensure_ascii=False)

        def walk(node, path="$"):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotEqual(key, "forecast", f"'forecast' key leaked at {path}")
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(self.report)
        values = json.loads(VALUES.read_text(encoding="utf-8"))
        sample = [f"{row['value']:.4f}" for row in values["series"][:5] if row.get("code") == "drgdp2"]
        for number in sample:
            self.assertNotIn(number, text)
        spf = self.report["sources"]["SPF_GDP_MEDIAN"]
        self.assertFalse(spf["values"]["values_published"])
        self.assertIsNone(spf["values"]["sample"])
        # the NY Fed licence allows a sample, but the project policy still keeps all third-party
        # forecast/actual values outside the committed report
        nyfed = self.report["sources"]["NY_FED_STAFF_NOWCAST"]
        self.assertFalse(nyfed["values"]["values_published"])
        self.assertIsNone(nyfed["values"]["sample"])
        self.assertIn("values_published", self.report["sources"]["ATLANTA_FED_GDPNOW"]["values"])

    def test_two_scorable_sources_meet_the_m2_requirement(self):
        report = self.report
        cross = report["cross_source"]
        self.assertGreaterEqual(cross["scored_sources_count"], sc.M2_MIN_SCORED_SOURCES)
        self.assertIn("SPF_GDP_MEDIAN", cross["scored_sources"])
        self.assertIn("NY_FED_STAFF_NOWCAST", cross["scored_sources"])
        self.assertTrue(cross["criteria_pass"]["m2_sources"])
        self.assertTrue(cross["criteria_pass"]["points"])
        self.assertGreaterEqual(cross["scored_points_total"], sc.MIN_SCORED_POINTS)
        # 2 >= 2 (M2) but still below the stricter docs/173 MVP target of 3
        if cross["scored_sources_count"] < sc.MIN_SCORED_SOURCES:
            self.assertFalse(cross["criteria_pass"]["sources"])

    def test_nyfed_archive_is_partial_and_not_overclaimed(self):
        block = self.report["sources"]["NY_FED_STAFF_NOWCAST"]["targets"][sc.TARGET_T2]["0"]
        w1 = block["windows"]["W1"]
        w2 = block["windows"]["W2"]
        # W1 is entirely historical reconstruction / no publication before the origin
        self.assertEqual(w1["scored"], 0)
        self.assertEqual(w1["exclusions"].get("RECONSTRUCTED_NOT_REAL_TIME"), 60)
        self.assertEqual(w1["exclusions"].get("NO_PUBLICATION_BEFORE_ORIGIN"), 144)
        # only as-published 2016-2019 rows are scorable inside W2; 2007-2015 are reconstructions
        self.assertEqual(w2["scored"], 48)
        self.assertEqual(w2["exclusions"].get("RECONSTRUCTED_NOT_REAL_TIME"), 108)
        self.assertEqual(w2["lookahead_excluded"], 0)
        self.assertLess(w2["coverage_share"], 0.5)

    def test_report_is_deterministic(self):
        again = sc.build_report(horizons=(0,), generated_at="2026-11-18T00:00:00Z")
        self.assertEqual(self.report["content_sha256"], again["content_sha256"])
        other_stamp = sc.build_report(horizons=(0,), generated_at="2030-01-01T00:00:00Z")
        self.assertEqual(self.report["content_sha256"], other_stamp["content_sha256"],
                         "generated_at must be excluded from the content digest")

    def test_status_is_honest_about_the_missing_sources(self):
        report = self.report
        cross = report["cross_source"]
        self.assertEqual(report["status"], "PARTIAL")
        self.assertIn("SCORED_SOURCES_BELOW_CRITERION(2<3)", report["status_reasons"])
        self.assertFalse(cross["criteria_pass"]["sources"])
        self.assertTrue(cross["criteria_pass"]["m2_sources"])
        self.assertEqual(cross["scored_sources_count"], 2)
        self.assertEqual(cross["licence_compliance"]["restricted_used"], [])
        self.assertEqual(cross["licence_compliance"]["values_published"], [])
        self.assertEqual(report["sources"]["ATLANTA_FED_GDPNOW"]["status"]["status"],
                         "RESTRICTED_NOT_USED")

    def test_documented_spf_availability(self):
        plan = sc.check_plan(data_root=ROOT / "data" / "raw", horizons=(0, 1))
        spf = plan["sources"]["SPF_GDP_MEDIAN"]
        h0 = spf["points"]["by_target"][f"{sc.TARGET_T2}/h0"]
        h1 = spf["points"]["by_target"][f"{sc.TARGET_T2}/h1"]
        self.assertEqual(h0["eligible"], 360)   # protocol v5: 204 W1 + 156 W2 month ends
        self.assertEqual(h0["available"], 117)  # docs/95 §2: 117 of 360 real-time SPF points
        self.assertAlmostEqual(h0["coverage_share"], 0.325, places=3)
        self.assertEqual(h1["eligible"], 118)   # quarter-end origins, protocol t2_origins
        self.assertEqual(h1["available"], 116)  # docs/95 §2: 116 by the release calendar
        self.assertEqual(plan["counts_mode"], "available")
        # the publishable facts are not loaded by --check at all
        self.assertFalse(spf["values_published"])


class StatusTests(unittest.TestCase):
    def test_status_thresholds(self):
        sources = {key: {} for key in ("A", "B", "C")}
        self.assertEqual(sc._status_of(sources, 0, [])[0], "NOT_AVAILABLE")
        self.assertEqual(sc._status_of(sources, 60, ["A"])[0], "PARTIAL")
        self.assertEqual(sc._status_of(sources, 40, ["A", "B", "C"])[0], "PARTIAL")
        self.assertEqual(sc._status_of(sources, 60, ["A", "B", "C"]), ("OK", []))


class CliTests(TempDirMixin):
    def test_check_mode_reports_not_available_for_an_empty_data_root(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sc.main(["--check", "--data-root", str(self.tmp), "--horizons", "0"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["report_id"], "AMFE-EXTERNAL-FORECAST-SCORECARD")
        self.assertEqual(payload["status"], "NOT_AVAILABLE")
        self.assertEqual(payload["sources"]["SPF_GDP_MEDIAN"]["status"]["status"], "UNAVAILABLE")
        self.assertFalse(payload["sources"]["SPF_GDP_MEDIAN"]["values_published"])

    def test_full_run_writes_the_report_and_never_raw_values(self):
        out = self.tmp / "scorecard.json"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = sc.main(["--data-root", str(self.tmp), "--horizons", "0",
                            "--max-origins", "2", "--out", str(out)])
        self.assertEqual(code, 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "NOT_AVAILABLE")
        self.assertEqual(json.loads(buffer.getvalue())["status"], "NOT_AVAILABLE")
        # the licence terms are metadata and may be quoted; forecast values are not written at all
        for source in payload["sources"].values():
            self.assertFalse(source["values"]["values_published"])
            self.assertNotIn("forecast", json.dumps(source["targets"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
