"""Tests for tools/world_reports.py (docs/131): schema, aggregates, determinism, fail-closed, CLI.

Everything runs on small deterministic synthetic tables (no third-party data); one test checks that
the reports are numerically consistent with the `world_pipeline` report for the same table.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import world_pipeline as wp  # noqa: E402
import world_reports as wr  # noqa: E402

SMALL = dict(countries=20, industries=10, seed=7)


def args(**overrides):
    namespace = wr.build_parser().parse_args([])
    namespace.synthetic = True
    namespace.root = "unused"
    namespace.dataset = "unused"
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def small_table():
    return wp.synthetic_table(SMALL["countries"], SMALL["industries"], seed=SMALL["seed"],
                              foreign_entries=2, workers=1)


def small_specs(table):
    return wr.scenario_specs(energy_shares=(0.05,), energy_industries=table.industries[:3],
                             import_shares=(0.10,), fx_countries=(table.countries[0],),
                             fx_shares=(0.10,))


def forbidden_keys(node, path=""):
    """Collect raw-data keys anywhere in the report (aggregates only)."""
    found = []
    raw_names = {"Z", "x", "y", "va", "quantiles", "delta_A", "delta_final_demand_vector"}
    if isinstance(node, dict):
        for key, value in node.items():
            if key in raw_names:
                found.append(f"{path}/{key}")
            found.extend(forbidden_keys(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(forbidden_keys(value, f"{path}[{index}]"))
    return found


class SpecsAndTopLists(unittest.TestCase):
    def test_scenario_specs_order_ids_and_parameters(self):
        specs = wr.scenario_specs(energy_shares=(0.05, -0.50),
                                  energy_industries=("CPA_B", "CPA_D35"),
                                  import_shares=(0.10, 0.20), fx_countries=("DE", "FR"),
                                  fx_shares=(0.10, 0.20))
        ids = [spec["id"] for spec in specs]
        self.assertEqual(ids, ["energy_minus5", "energy_plus50", "import_plus10",
                               "import_plus20", "fx_minus10_DE", "fx_minus20_DE",
                               "fx_minus10_FR", "fx_minus20_FR"])
        self.assertEqual(specs[0]["parameters"]["industries"], ["CPA_B", "CPA_D35"])
        self.assertEqual(specs[1]["parameters"]["share"], -0.50)
        self.assertEqual(specs[4]["parameters"]["currency_change"], -0.10)
        self.assertEqual(specs[4]["parameters"]["pass_through"], 0.10)
        with self.assertRaises(wr.ReportError):
            wr.scenario_specs(energy_shares=(1.5,))
        with self.assertRaises(wr.ReportError):
            wr.scenario_specs(energy_shares=(0.0,))
        with self.assertRaises(wr.ReportError):
            wr.scenario_specs(import_shares=(0.0,))

    def test_specs_checked_against_the_table(self):
        table = small_table()
        good = wr.scenario_specs(energy_industries=table.industries[:2],
                                 import_shares=(0.1,), fx_countries=(table.countries[0],),
                                 fx_shares=(0.1,))
        wr._validate_specs_against(table, good)  # does not raise
        with self.assertRaises(wr.ReportError):
            wr._validate_specs_against(table, wr.scenario_specs(
                energy_industries=("NOPE",), import_shares=(0.1,), fx_countries=(), fx_shares=()))
        with self.assertRaises(wr.ReportError):
            wr._validate_specs_against(table, wr.scenario_specs(
                energy_industries=table.industries[:1], import_shares=(0.1,),
                fx_countries=("NOPE",), fx_shares=(0.1,)))

    def test_top_list_is_sorted_by_absolute_change_with_names(self):
        mapping = {"B": -10.0, "A": 5.0, "C": 10.0, "D": 1.0}
        top = wr.top_list(mapping, n=3)
        self.assertEqual([entry["name"] for entry in top["top"]], ["B", "C", "A"])
        self.assertEqual(top["largest"], "B")
        self.assertAlmostEqual(top["top_share_of_abs"], 25.0 / 26.0, places=6)
        self.assertEqual(top["entries"], 4)
        self.assertEqual(wr.top_list({})["top"], [])
        # aggregate rows can be excluded from the ranking without changing the total
        named = wr.top_list({"ROW": -100.0, "B": -10.0, "A": 5.0}, n=2, exclude=("ROW",))
        self.assertEqual([entry["name"] for entry in named["top"]], ["B", "A"])
        self.assertEqual(named["largest"], "B")
        self.assertEqual(named["excluded"], ["ROW"])
        self.assertAlmostEqual(named["total"], 115.0, places=6)


class Reports(unittest.TestCase):
    def setUp(self):
        self.table = small_table()
        self.specs = small_specs(self.table)
        self.meta = {"dataset_id": "synthetic", "sha256": "-", "countries": self.table.n_countries,
                     "industries": self.table.n_industries, "sectors": self.table.n,
                     "stored_Z": int(self.table.Z.nnz), "stored_share": 0.1, "years": [2020],
                     "units": "USD_mn", "total_output": 1.0, "total_final_demand": 1.0,
                     "total_value_added": 1.0}
        self.reports, self.index, self.context = wr.build_reports(self.table, self.specs, self.meta)

    def test_report_schema_and_headline_numbers(self):
        self.assertEqual(len(self.reports), 3)
        for report in self.reports:
            self.assertEqual(report["schema"], wr.REPORT_SCHEMA)
            self.assertEqual(report["status"], wr.STATUS)
            self.assertIn("не допуск", report["status_note"])
            self.assertIn("scenario", report)
            self.assertIn("baseline", report)
            self.assertIn("checks", report)
            self.assertEqual(report["result"]["units"], "USD_mn")
            self.assertEqual(report["impacts"]["countries"]["entries"], self.table.n_countries)
            self.assertEqual(report["impacts"]["industries"]["entries"], self.table.n_industries)
        energy = self.reports[0]
        self.assertEqual(energy["scenario"]["id"], "energy_minus5")
        self.assertLess(energy["result"]["total_delta_output"], 0.0)
        self.assertIsNotNone(energy["result"]["output_multiplier_vs_demand"])
        # energy impacts aggregate to the reported total
        self.assertAlmostEqual(sum(energy["impacts"]["by_country"].values()),
                               energy["result"]["total_delta_output"], places=1)
        self.assertAlmostEqual(sum(energy["impacts"]["by_industry"].values()),
                               energy["result"]["total_delta_output"], places=1)
        # every scenario checks both balances
        for report in self.reports:
            self.assertTrue(report["checks"]["validation_pass"])
            self.assertLess(report["checks"]["scenario_material_residual"], 1e-6)
            self.assertTrue(report["checks"]["feasible"])

    def test_reports_contain_no_raw_data(self):
        payload = json.dumps([report for report in self.reports], ensure_ascii=False)
        self.assertEqual(forbidden_keys(self.reports), [])
        for report in self.reports:
            for key in ("impacts", "baseline", "result"):
                block = report[key]
                self.assertIsInstance(block, dict)
            # aggregates are vectors of countries/industries, never of sectors
            self.assertLessEqual(len(report["impacts"]["by_country"]), self.table.n_countries)
            self.assertLessEqual(len(report["impacts"]["by_industry"]), self.table.n_industries)
            self.assertNotIn("by_sector", report["impacts"])
        self.assertNotIn('"Z"', payload)
        self.assertLess(len(payload), 200_000)  # aggregate-only size

    def test_index_schema_and_dashboard(self):
        self.assertEqual(self.index["schema"], wr.INDEX_SCHEMA)
        self.assertEqual(self.index["status"], wr.STATUS)
        self.assertEqual(len(self.index["scenarios"]), 3)
        self.assertTrue(self.index["checks"]["all_validations_pass"])
        self.assertTrue(self.index["checks"]["all_feasible"])
        for entry in self.index["scenarios"]:
            self.assertTrue(entry["file_json"].startswith("reports/"))
            self.assertTrue(entry["file_md"].startswith("reports/"))
        with tempfile.TemporaryDirectory() as tmp:
            paths = wr.write_outputs(self.reports, self.index, Path(tmp))
            for entry in self.index["scenarios"]:
                self.assertTrue(Path(paths["reports"][entry["id"]]["json"]).is_file())
                self.assertTrue(Path(paths["reports"][entry["id"]]["md"]).is_file())
            html_text = Path(paths["dashboard"]["html"]).read_text(encoding="utf-8")
            md_text = Path(paths["dashboard"]["md"]).read_text(encoding="utf-8")
            self.assertNotIn("<script", html_text.lower())
            self.assertNotIn("http://", html_text.lower())
            self.assertNotIn("https://", html_text.lower())
            self.assertIn("не допуск", html_text)
            self.assertIn(wr.STATUS, html_text)
            self.assertIn("не допуск", md_text)
            for entry in self.index["scenarios"]:
                self.assertIn(entry["file_md"], html_text)

    def test_markdown_report_rendering(self):
        report = self.reports[0]
        text = wr.render_scenario_markdown(report)
        self.assertIn("# Сценарий `energy_minus5`", text)
        self.assertIn("Топ-10 стран", text)
        self.assertIn("Топ-10 отраслей", text)
        self.assertIn("Проверки", text)
        self.assertIn("Ограничения", text)
        self.assertIn("не допуск", text)

    def test_consistency_with_pipeline_report(self):
        pipeline_args = wp.build_parser().parse_args([])
        pipeline_args.synthetic = True
        pipeline_args.countries = self.table.n_countries
        pipeline_args.industries = self.table.n_industries
        pipeline_args.seed = SMALL["seed"]
        pipeline_args.foreign_entries = 2
        pipeline_args.workers = 1
        pipeline_args.scenario = "energy"
        pipeline_args.share = 0.05
        pipeline_report = wp.run_pipeline(pipeline_args)
        energy = self.reports[0]
        self.assertEqual(pipeline_report["validation"]["max_abs_row_residual"],
                         energy["checks"]["max_abs_row_residual"])
        self.assertAlmostEqual(pipeline_report["solve"]["total_output"],
                               energy["baseline"]["total_output"], places=1)
        self.assertAlmostEqual(pipeline_report["scenario"]["result"]["total_delta_output"],
                               energy["result"]["total_delta_output"], places=1)
        self.assertEqual(pipeline_report["core"]["spectral_radius"],
                         energy["checks"]["country_block_spectral_radius"])
        self.assertEqual(pipeline_report["solve"]["spectral_radius"],
                         energy["checks"]["baseline_spectral_radius"])


class DeterminismAndFailClosed(unittest.TestCase):
    def test_determinism_across_runs(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = wr.run(args(out=first_dir))
            second = wr.run(args(out=second_dir))
            for scenario_id in first["paths"]["reports"]:
                for kind in ("json", "md"):
                    left = Path(first["paths"]["reports"][scenario_id][kind]).read_bytes()
                    right = Path(second["paths"]["reports"][scenario_id][kind]).read_bytes()
                    self.assertEqual(left, right)
            left_html = Path(first["paths"]["dashboard"]["html"]).read_bytes()
            right_html = Path(second["paths"]["dashboard"]["html"]).read_bytes()
            self.assertEqual(left_html, right_html)
            left_index = json.loads(Path(first["paths"]["index"]).read_text(encoding="utf-8"))
            right_index = json.loads(Path(second["paths"]["index"]).read_text(encoding="utf-8"))
            left_index.pop("timings_ms")
            right_index.pop("timings_ms")
            self.assertEqual(left_index, right_index)

    def test_fail_closed_refusal_writes_nothing(self):
        table = small_table()
        self.specs = small_specs(table)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wp, "scenario_import",
                                   side_effect=wp.PipelineError("shocked technology not productive")):
                with self.assertRaises(wr.ReportError):
                    wr.run(args(out=tmp, import_shares="0.10", fx_shares="0.10",
                                fx_countries=table.countries[0],
                                energy_industries=table.industries[0]))
                self.assertFalse((Path(tmp) / "report_index.json").exists())
                self.assertFalse((Path(tmp) / "reports").exists())

    def test_fail_closed_unknown_industry_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(wr.ReportError):
                wr.run(args(out=tmp, energy_industries="NOPE", import_shares="0.10",
                            fx_shares="0.10", fx_countries="C01"))
            self.assertFalse((Path(tmp) / "report_index.json").exists())


class Cli(unittest.TestCase):
    def test_cli_writes_artefacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = wr.main(["--synthetic", "--countries", "20", "--industries", "10",
                            "--import-shares", "0.10", "--fx-countries", "C01",
                            "--fx-shares", "0.10", "--out", tmp])
            self.assertEqual(code, wr.EXIT_OK)
            index = json.loads((Path(tmp) / "report_index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["schema"], wr.INDEX_SCHEMA)
            self.assertTrue((Path(tmp) / "dashboard.html").is_file())
            for entry in index["scenarios"]:
                self.assertTrue((Path(tmp) / entry["file_json"]).is_file())
                self.assertTrue((Path(tmp) / entry["file_md"]).is_file())

    def test_cli_blocks_without_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = wr.main(["--root", tmp, "--dataset", "does-not-exist", "--out", tmp])
            self.assertEqual(code, wr.EXIT_BLOCKED)

    def test_cli_invalid_industry_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = wr.main(["--synthetic", "--countries", "20", "--industries", "10",
                            "--energy-industries", "NOPE", "--out", tmp])
            self.assertEqual(code, wr.EXIT_INVALID)


class SFCBlock(unittest.TestCase):
    """SFC <-> IO block (docs/133) in the scenario reports, index and dashboard."""

    def _run(self, **overrides):
        table = small_table()
        overrides.setdefault("energy_industries", ",".join(table.industries[:2]))
        overrides.setdefault("import_shares", "0.10")
        overrides.setdefault("fx_countries", table.countries[0])
        overrides.setdefault("fx_shares", "0.10")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = wr.run(args(out=tmp.name, **overrides))
        return result, Path(tmp.name)

    def test_flag_adds_sfc_block_to_reports_index_and_dashboard(self):
        result, out_dir = self._run(sfc=True)
        for report in result["reports"]:
            self.assertIn("sfc", report)
            block = report["sfc"]
            self.assertEqual(block["schema"], wr.SFC_SCHEMA)
            self.assertTrue(block["pass"])
            self.assertGreater(block["countries"], 0)
            for key in ("gdp", "final_demand", "savings", "investment", "current_account"):
                self.assertIn(key, block["totals"])
            self.assertEqual(forbidden_keys(block), [])
        index = result["index"]
        self.assertIsNotNone(index["sfc"])
        self.assertTrue(index["checks"]["all_sfc_pass"])
        for entry in index["scenarios"]:
            self.assertTrue(entry["checks"]["sfc_block_pass"])
        markdown = Path(result["paths"]["reports"]["energy_minus5"]["md"]).read_text(encoding="utf-8")
        self.assertIn("SFC-проверки (S = I + CA)", markdown)
        dashboard_md = (out_dir / "dashboard.md").read_text(encoding="utf-8")
        dashboard_html = (out_dir / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("SFC-проверки (S = I + CA)", dashboard_md)
        self.assertIn("SFC-проверки (S = I + CA)", dashboard_html)
        json.loads((out_dir / "report_index.json").read_text(encoding="utf-8"))

    def test_without_flag_the_artefacts_are_unchanged(self):
        result, out_dir = self._run(sfc=False)
        for report in result["reports"]:
            self.assertNotIn("sfc", report)
        self.assertIsNone(result["index"].get("sfc"))
        self.assertIsNone(result["index"]["checks"].get("all_sfc_pass"))
        dashboard_md = (out_dir / "dashboard.md").read_text(encoding="utf-8")
        self.assertNotIn("SFC-проверки", dashboard_md)

    def test_totals_are_consistent_with_saving_investment_identity(self):
        result, _ = self._run(sfc=True)
        sfc = result["index"]["sfc"]
        totals = sfc["totals"]
        checks = sfc["checks"]
        self.assertAlmostEqual(totals["gdp"], totals["final_demand"], places=4)
        self.assertAlmostEqual(totals["savings"] - totals["investment"],
                               totals["current_account"], places=4)
        scale = max(1.0, abs(totals["gdp"]))
        self.assertLessEqual(abs(checks["global_current_account_sum"]), 1e-6 * scale)
        self.assertLessEqual(checks["saving_investment_identity_max_abs"], 1e-6 * scale)
        self.assertLessEqual(checks["current_account_disagreement_max_abs"], 1e-6 * scale)
        self.assertTrue(checks["sfc_pass"])
        self.assertLessEqual(abs(checks["net_foreign_assets_closing_sum"]), 1e-6 * scale)
        # the block is baseline accounting: the same numbers in every scenario report
        first = result["reports"][0]["sfc"]["totals"]
        for report in result["reports"][1:]:
            self.assertEqual(report["sfc"]["totals"], first)

    def test_investment_share_changes_split_not_the_identity(self):
        low, _ = self._run(sfc=True, sfc_investment_share=0.0)
        high, _ = self._run(sfc=True, sfc_investment_share=0.4)
        low_totals = low["index"]["sfc"]["totals"]
        high_totals = high["index"]["sfc"]["totals"]
        self.assertAlmostEqual(low_totals["savings"], low_totals["investment"], places=4)
        self.assertAlmostEqual(high_totals["savings"] - high_totals["investment"],
                               high_totals["current_account"], places=4)
        self.assertAlmostEqual(high_totals["current_account"], low_totals["current_account"],
                               places=4)
        self.assertNotAlmostEqual(high_totals["savings"], low_totals["savings"], places=1)
        self.assertTrue(low["index"]["sfc"]["pass"])
        self.assertTrue(high["index"]["sfc"]["pass"])

    def test_sfc_block_is_deterministic(self):
        first, _ = self._run(sfc=True)
        second, _ = self._run(sfc=True)
        self.assertEqual(json.dumps(first["index"]["sfc"], sort_keys=True),
                         json.dumps(second["index"]["sfc"], sort_keys=True))
        for left, right in zip(first["reports"], second["reports"]):
            self.assertEqual(json.dumps(left["sfc"], sort_keys=True),
                             json.dumps(right["sfc"], sort_keys=True))

    def test_sfc_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = small_table()
            with mock.patch.object(wr, "sfc_section",
                                   side_effect=wr.SFCIOError("broken IO accounting")):
                with self.assertRaises(wr.SFCIOError):
                    wr.run(args(sfc=True, out=tmp, import_shares="0.10", fx_shares="0.10",
                                fx_countries=table.countries[0],
                                energy_industries=table.industries[0]))
            self.assertFalse((Path(tmp) / "report_index.json").exists())
            self.assertFalse((Path(tmp) / "reports").exists())

    def test_cli_sfc_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = wr.main(["--synthetic", "--countries", "20", "--industries", "10",
                            "--import-shares", "0.10", "--fx-countries", "C01",
                            "--fx-shares", "0.10", "--sfc", "--out", tmp])
            self.assertEqual(code, wr.EXIT_OK)
            index = json.loads((Path(tmp) / "report_index.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(index["sfc"])
            self.assertTrue(index["checks"]["all_sfc_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
