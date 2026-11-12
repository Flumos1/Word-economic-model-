"""Tests for the world-core calibration (``tools/calibrate_core.py``).

The suite is offline and standard-library only:

* synthetic fixtures with an exactly known expenditure structure (C=0.55, I=0.15, G=0.20,
  X=0.13, M=0.03 of GDP) so the share parameters and the V2 additivity checks have analytic
  expected values;
* report contract tests: structure, determinism, derived-statistics-only rule, provenance, the
  three-status V2 vocabulary and the explicit NOT_APPLICABLE rows;
* failure modes: missing required series (strict error vs. PARTIAL report);
* repository coverage: every calibration series is in ``protocols/data_plan.json`` with an OPEN
  register entry and the dense 147-date vintage grid.
"""
from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import calibrate_core as cal  # noqa: E402

REQUIRED = cal.REQUIRED
OPTIONAL = cal.OPTIONAL
QUARTER_STARTS = ("01-01", "04-01", "07-01", "10-01")
SHARES = {"PCECC96": 0.55, "GPDIC1": 0.15, "GCEC1": 0.20, "EXPGSC1": 0.13, "IMPGSC1": 0.03}
MONTHLY_SENTINEL = 987654.321
NAIRU_RAW = 5.123456


def growth_path(first_year: int = 1990, last_year: int = 2024) -> list:
    """Quarterly real GDP growth rates (deterministic, positive, varying)."""
    return [0.01 + 0.002 * math.sin(index / 7.0)
            for index in range(4 * (last_year - first_year + 1))]


def quarter_dates(first_year: int = 1990, last_year: int = 2024) -> list:
    dates = []
    for year in range(first_year, last_year + 1):
        for tail in QUARTER_STARTS:
            dates.append(f"{year}-{tail}")
    return dates


def month_dates(first_year: int = 1990, last_year: int = 2024) -> list:
    return [f"{year}-{month:02d}-01" for year in range(first_year, last_year + 1)
            for month in range(1, 13)]


def write_series(root: Path, series_id: str, rows: list) -> None:
    folder = root / series_id
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["observation_date," + series_id]
    lines += [f"{date},{value}" for date, value in rows]
    (folder / "latest.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_fixture(root: Path, consistent: bool = True) -> None:
    """Deterministic synthetic data with an exactly known expenditure structure.

    GDP grows 1% per quarter, payrolls are constant at 100, so real output per employee grows 1%
    per quarter (4.0604% per year).  The components are exactly additive when ``consistent`` is
    true; otherwise the four components sum to 0.9 of GDP, which the V2 check must detect.
    """
    quarters = quarter_dates()
    scale = 0.5 if not consistent else 1.0
    growth = growth_path()
    level, gdp = 100.0, []
    for rate in growth:
        gdp.append(level)
        level *= 1.0 + rate
    # GDPC1
    write_series(root, "GDPC1", list(zip(quarters, gdp)))
    for series_id, share in SHARES.items():
        write_series(root, series_id, [(date, share * value * scale)
                                       for date, value in zip(quarters, gdp)])
    # Implied private consumption is not a series in the plan; PCECC96 is the C component.
    published = [((1.0 + growth[0]) ** 4 - 1.0) * 100.0]
    published += [((1.0 + growth[index - 1]) ** 4 - 1.0) * 100.0
                  for index in range(1, len(quarters))]
    write_series(root, "A191RL1Q225SBEA", list(zip(quarters, published)))
    indpro, level = [], 50.0
    for rate in growth:
        indpro.append(level)
        level *= 1.0 + rate
    write_series(root, "INDPRO", list(zip(quarters, indpro)))
    write_series(root, "PAYEMS", [(date, 100.0) for date in month_dates()])
    # Prices: deterministic varying inflation so the correlations are defined.
    for series_id, base, amplitude in (("CPIAUCSL", 0.005, 0.003),
                                       ("PCEPILFE", 0.004, 0.0024),
                                       ("GDPDEF", 0.0045, 0.0027)):
        level = 100.0
        rows = []
        for index, date in enumerate(quarters):
            level *= 1.0 + base + amplitude * math.sin(index / 5.0)
            rows.append((date, level))
        write_series(root, series_id, rows)
    # Unemployment: a flat 2015-2019 window (the NAIRU proxy) inside a wider sample range.
    rows = []
    for date in month_dates():
        year = int(date[:4])
        value = 6.0 if year < 2015 else NAIRU_RAW if year <= 2019 else 4.0
        rows.append((date, value))
    write_series(root, "UNRATE", rows)
    # Optional series: a distinctive sentinel must never reach the committed report.
    write_series(root, "RSAFS", [(date, MONTHLY_SENTINEL + index)
                                 for index, date in enumerate(month_dates())])
    write_series(root, "GCE", list(zip(quarters, gdp)))


def calibrate(root: Path, **kwargs) -> dict:
    return cal.calibrate(root, **kwargs)


class CalibrationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "raw"
        build_fixture(cls.root)
        cls.report = calibrate(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def parameter(self, report: dict, pid: str) -> dict:
        for parameter in report["parameters"]:
            if parameter["id"] == pid:
                return parameter
        raise AssertionError(f"parameter {pid!r} missing")

    def check(self, report: dict, check_id: str) -> dict:
        for item in report["v2_checks"]:
            if item["id"] == check_id:
                return item
        raise AssertionError(f"V2 check {check_id!r} missing")

    def test_report_structure(self):
        report = self.report
        for key in ("calibration_id", "version", "tool", "protocol", "plan", "generated_at",
                    "status", "purpose", "raw_data_committed", "reproduce", "windows", "sources",
                    "parameters", "derived_aggregates", "v2_checks", "criteria", "limitations",
                    "next_steps", "content_sha256"):
            self.assertIn(key, report)
        self.assertEqual(report["calibration_id"], "AMFE-CORE-CALIBRATION")
        self.assertEqual(report["status"], "COMPLETE")
        self.assertFalse(report["raw_data_committed"])
        ids = [parameter["id"] for parameter in report["parameters"]]
        self.assertEqual(ids, ["gov_share", "investment_share", "import_share", "export_share",
                               "consumption_share", "nairu_proxy", "productivity_trend"])
        for parameter in report["parameters"]:
            self.assertIn(parameter["status"], {"CALIBRATED", "UNAVAILABLE"})
            self.assertIn("unit", parameter)
            self.assertIn("windows", parameter)
            self.assertIn("sources", parameter)
        self.assertGreaterEqual(len(report["v2_checks"]), 6)
        for check in report["v2_checks"]:
            self.assertIn(check["status"], {"PASS", "CONDITIONAL", "NOT_APPLICABLE"}, check["id"])
        self.assertGreaterEqual(len(report["limitations"]), 3)
        self.assertGreaterEqual(len(report["next_steps"]), 2)

    def test_share_parameters_match_the_analytic_structure(self):
        expected = {"gov_share": 0.20, "investment_share": 0.15, "import_share": 0.03,
                    "export_share": 0.13, "consumption_share": 0.55}
        for pid, value in expected.items():
            parameter = self.parameter(self.report, pid)
            self.assertAlmostEqual(parameter["value"], value, places=6, msg=pid)
            self.assertEqual(parameter["windows"]["baseline"]["quarters"], 120)
            self.assertAlmostEqual(parameter["windows"]["recent"]["mean"], value, places=6)
        derived = self.report["derived_aggregates"]
        self.assertAlmostEqual(derived["net_export_share_baseline"], 0.10, places=6)
        self.assertAlmostEqual(derived["implied_consumption_share_baseline"], 0.55, places=6)
        self.assertEqual(derived["baseline_window"], ["1990Q1", "2019Q4"])

    def test_nairu_proxy_and_productivity_trend(self):
        nairu = self.parameter(self.report, "nairu_proxy")
        self.assertAlmostEqual(nairu["value"], round(NAIRU_RAW, 4), places=4)
        self.assertEqual(nairu["windows"]["proxy"]["months"], 60)
        productivity = self.parameter(self.report, "productivity_trend")
        levels, level = [], 100.0
        for rate in growth_path()[:120]:
            levels.append(level)
            level *= 1.0 + rate
        slope = cal.ols_slope(list(range(len(levels))),
                              [math.log(value / 100.0) for value in levels])
        expected = (math.exp(4.0 * slope) - 1.0) * 100.0
        self.assertAlmostEqual(productivity["value"], expected, places=3)
        self.assertIsNotNone(productivity["windows"]["pre_2008"]["annual_growth_percent"])
        self.assertIsNotNone(productivity["windows"]["post_2010"]["annual_growth_percent"])

    def test_v2_additivity_levels_and_growth_pass_on_exact_fixture(self):
        levels = self.check(self.report, "V2_additivity_levels")
        self.assertEqual(levels["status"], "PASS")
        self.assertLess(levels["statistic"]["max_abs_residual"], 1e-9)
        self.assertEqual(levels["statistic"]["quarters"], 120)
        growth = self.check(self.report, "V2_additivity_growth")
        self.assertEqual(growth["status"], "PASS")
        self.assertLess(growth["statistic"]["max_abs_residual"], 1e-9)

    def test_v2_growth_series_agrees_with_published_growth(self):
        check = self.check(self.report, "V2_gdp_growth_series")
        self.assertEqual(check["status"], "PASS")
        self.assertLess(check["statistic"]["max_abs_difference_pp"], 1e-6)
        self.assertEqual(check["statistic"]["quarters"], 119)  # 120 quarters, 119 growth pairs

    def test_v2_productivity_cross_check(self):
        check = self.check(self.report, "V2_productivity_xcheck")
        self.assertEqual(check["status"], "PASS")
        self.assertGreaterEqual(check["statistic"]["correlation_yoy"], 0.99)

    def test_not_applicable_rows_are_explicit(self):
        for check_id in ("V2_structural_parameters", "V2_world_aggregation", "V2_nominal_identity"):
            check = self.check(self.report, check_id)
            self.assertEqual(check["status"], "NOT_APPLICABLE", check_id)
            self.assertTrue(check["reason"], check_id)
        thresholds = self.report["criteria"]["v2_thresholds"]
        self.assertIn("additivity_levels_max", thresholds)
        self.assertIn("gdp_growth_mean_abs", thresholds)

    def test_no_raw_values_in_the_report(self):
        text = json.dumps(self.report, ensure_ascii=False)
        self.assertNotIn(str(MONTHLY_SENTINEL), text, "optional raw series leaked into the report")
        self.assertNotIn(str(NAIRU_RAW), text, "raw UNRATE values leaked into the report")
        self.assertNotIn("1.01 **", text)

        def numbers(value):
            if isinstance(value, bool):
                return
            if isinstance(value, (int, float)):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from numbers(item)
            elif isinstance(value, list):
                if value and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                                 for item in value):
                    self.assertLessEqual(len(value), 12,
                                         "the report must carry aggregates, not observation arrays")
                for item in value:
                    yield from numbers(item)

        list(numbers(self.report))

    def test_provenance_fields(self):
        for source in self.report["sources"]:
            self.assertIn(source["series"], REQUIRED + OPTIONAL)
            self.assertRegex(source["file_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(source["rows"], 0)
            self.assertTrue(source["file"].endswith(f"{source['series']}/latest.csv"))
            self.assertIsNotNone(source["register"], source["series"])
        self.assertRegex(self.report["content_sha256"], r"^[0-9a-f]{64}$")

    def test_determinism(self):
        second = calibrate(self.root)
        first_copy = copy.deepcopy(self.report)
        first_copy.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first_copy, second, "the calibration must be deterministic on fixed data")
        self.assertEqual(self.report["content_sha256"], second["content_sha256"])


class CalibrationFailureModes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "raw"
        build_fixture(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_series_strict_raises(self):
        for series_id in ("PAYEMS", "GDPC1", "UNRATE"):
            path = self.root / series_id / "latest.csv"
            path.unlink()
            with self.assertRaises(cal.CalibrationError, msg=series_id):
                calibrate(self.root)
            build_fixture(self.root)

    def test_missing_series_allow_missing_is_partial(self):
        for series_id in ("PAYEMS", "INDPRO"):
            (self.root / series_id / "latest.csv").unlink()
        report = calibrate(self.root, allow_missing=True)
        self.assertEqual(report["status"], "PARTIAL")
        parameters = {parameter["id"]: parameter for parameter in report["parameters"]}
        self.assertEqual(parameters["productivity_trend"]["status"], "UNAVAILABLE")
        self.assertEqual(parameters["gov_share"]["status"], "CALIBRATED")
        checks = {check["id"]: check for check in report["v2_checks"]}
        self.assertEqual(checks["V2_productivity_xcheck"]["status"], "NOT_APPLICABLE")
        self.assertEqual(checks["V2_additivity_levels"]["status"], "PASS")
        self.assertEqual(report["missing_series"], ["INDPRO", "PAYEMS"])

    def test_empty_data_root_raises_without_allow_missing(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(cal.CalibrationError):
            calibrate(empty)
        report = calibrate(empty, allow_missing=True)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual({parameter["status"] for parameter in report["parameters"]}, {"UNAVAILABLE"})
        self.assertTrue(all(check["status"] == "NOT_APPLICABLE" for check in report["v2_checks"]))


class CalibrationRejectsBadData(unittest.TestCase):
    def test_v2_additivity_flags_inconsistent_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            build_fixture(root, consistent=False)
            report = calibrate(root)
        checks = {check["id"]: check for check in report["v2_checks"]}
        levels = checks["V2_additivity_levels"]
        self.assertEqual(levels["status"], "CONDITIONAL")
        self.assertAlmostEqual(levels["statistic"]["max_abs_residual"], 0.5, places=6)
        growth = checks["V2_additivity_growth"]
        self.assertEqual(growth["status"], "CONDITIONAL")
        self.assertGreater(growth["statistic"]["median_abs_residual"], 0.002)

    def test_csv_parsing_and_calendar_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.csv"
            path.write_text("observation_date,SERIES\n2020-01-01,1.5\n2020-02-01,.\n"
                            "2020-03-01,\n2020-04-01,2.5\n", encoding="utf-8")
            values = cal.read_fred_csv(path)
        self.assertEqual(values, {"2020-01-01": 1.5, "2020-04-01": 2.5})
        self.assertEqual(cal.month_label(cal.month_index("2020-02-01")), "2020-02")
        self.assertEqual(cal.quarter_label(cal.quarter_index("2020-04-01")), "2020Q2")
        quarters = cal.to_quarters({"2020-01-01": 1.0, "2020-02-01": 2.0, "2020-03-01": 3.0})
        self.assertEqual(quarters, {cal.quarter_index("2020-01-01"): 2.0})
        with self.assertRaises(cal.CalibrationError):
            cal.read_fred_csv(Path(tmp) / "missing.csv")

    def test_cli_writes_report_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            build_fixture(root)
            out = Path(tmp) / "out" / "core_calibration.json"
            code = cal.main(["--data-root", str(root), "--out", str(out)])
            self.assertEqual(code, 0)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written["calibration_id"], "AMFE-CORE-CALIBRATION")
            empty = Path(tmp) / "empty"
            empty.mkdir()
            failed_out = Path(tmp) / "out" / "failed.json"
            code = cal.main(["--data-root", str(empty), "--out", str(failed_out)])
            self.assertEqual(code, 2)
            self.assertFalse(failed_out.exists())


class PlanCoverage(unittest.TestCase):
    def test_calibration_series_are_planned_registered_and_dense(self):
        plan = json.loads((ROOT / "protocols" / "data_plan.json").read_text(encoding="utf-8"))
        register = json.loads((ROOT / "protocols" / "licence_register.json").read_text(encoding="utf-8"))
        statuses = {entry["id"]: entry["status"] for entry in register["entries"]}
        series = {entry["id"]: entry for entry in plan["series"]}
        self.assertEqual(plan["version"], "0.4.0")
        for series_id in REQUIRED + OPTIONAL:
            self.assertIn(series_id, series, series_id)
            entry = series[series_id]
            self.assertIn(entry["register"], statuses, series_id)
            self.assertEqual(statuses[entry["register"]], "OPEN", series_id)
            self.assertTrue(entry["url"].startswith("https://fred.stlouisfed.org/"), series_id)
            self.assertEqual(len(entry["vintages"]), 147, series_id)
            self.assertEqual(entry["vintages"][0], "1990-03-31", series_id)
            self.assertEqual(entry["vintages"][-1], "2026-09-30", series_id)
            self.assertIn("licence_label", entry, series_id)
        # the retrieval-only chronology stays without vintage retrieval
        self.assertEqual(series["USREC"]["vintages"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
