"""SFC <-> IO bridge: country accounts, S=I+CA, net foreign asset period step, pipeline flag.

All tests are offline and use small synthetic tables built so that both IO identities hold exactly
(``x = Z 1 + y + fda`` and ``x = 1' Z + va + vaa``); a small FIGARO-shaped document (fixed country
codes, block layout) is used to check the block aggregation, and the world pipeline is exercised on
its synthetic table with and without ``--sfc``.
"""
import json
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.world import sfc_io  # noqa: E402
import world_pipeline  # noqa: E402


# --------------------------------------------------------------------------------------- fixtures
def build_block_matrix(countries, industries, domestic=0.10, cross_industry=0.03,
                       foreign=0.02):
    n_countries, n_industries = len(countries), len(industries)
    n = n_countries * n_industries
    A = np.zeros((n, n), dtype=float)
    for country in range(n_countries):
        for row in range(n_industries):
            for column in range(n_industries):
                A[country * n_industries + row][country * n_industries + column] = (
                    domestic if row == column else cross_industry)
            for other in range(n_countries):
                if other == country:
                    continue
                for column in range(n_industries):
                    A[country * n_industries + row][other * n_industries + column] = foreign
    return A


def synthetic_document(countries=("US", "DE", "CN"), industries=("A", "B"),
                       output=(100.0, 120.0, 90.0, 110.0, 130.0, 80.0),
                       adjustments=None) -> dict:
    n_countries, n_industries = len(countries), len(industries)
    n = n_countries * n_industries
    A = build_block_matrix(countries, industries)
    assert len(output) == n
    x = np.asarray(output, dtype=float)
    Z = A * x[None, :]
    y = (np.eye(n) - A) @ x
    va = x - Z.sum(axis=0)
    fda = np.zeros(n)
    vaa = np.zeros(n)
    if adjustments:
        fda = np.asarray(adjustments["fda"], dtype=float)
        vaa = np.asarray(adjustments["vaa"], dtype=float)
        x = Z.sum(axis=1) + y + fda
        va = x - Z.sum(axis=0) - vaa
    return {"countries": list(countries), "industries": list(industries),
            "Z": Z.tolist(), "y": y.tolist(), "x": x.tolist(), "va": va.tolist(),
            "final_demand_adjustments": fda.tolist(), "va_adjustments": vaa.tolist(),
            "units": "USD_mn"}


def sparse_document(document: dict) -> dict:
    """The same table in the schema 1.1 COO form ``Z_sparse`` (no dense Z)."""
    Z = np.asarray(document["Z"], dtype=float)
    rows, cols = np.nonzero(Z)
    out = {key: value for key, value in document.items() if key != "Z"}
    out["Z_sparse"] = {"rows": rows.tolist(), "cols": cols.tolist(),
                       "values": Z[rows, cols].tolist()}
    return out


def small_accounts(**kwargs):
    return sfc_io.country_accounts(synthetic_document(**kwargs))


# ---------------------------------------------------------------------------------------- flows
class CountryFlows(unittest.TestCase):
    def test_identities_hold_on_a_consistent_table(self):
        document = synthetic_document()
        flows = sfc_io.country_flows(document)
        self.assertEqual(list(flows["countries"]), ["US", "DE", "CN"])
        self.assertEqual(len(flows["gdp"]), 3)
        self.assertAlmostEqual(math.fsum(flows["gdp"]), math.fsum(flows["final_demand"]),
                               places=8)
        self.assertAlmostEqual(math.fsum(flows["current_account"]), 0.0, places=8)
        for index in range(3):
            self.assertAlmostEqual(
                flows["current_account"][index],
                flows["intermediate_exports"][index] - flows["intermediate_imports"][index],
                places=9)
            self.assertAlmostEqual(
                flows["current_account"][index],
                flows["gdp"][index] - flows["final_demand"][index], places=8)
        self.assertLess(flows["checks"]["current_account_disagreement_max_abs"], 1e-9)
        self.assertAlmostEqual(flows["intermediate_flow_total"],
                               float(np.asarray(document["Z"]).sum()), places=6)

    def test_dense_and_sparse_documents_agree(self):
        dense = sfc_io.country_flows(synthetic_document())
        sparse = sfc_io.country_flows(sparse_document(synthetic_document()))
        self.assertEqual(list(sparse["countries"]), list(dense["countries"]))
        self.assertEqual(sparse["storage"], "sparse_Z")
        for key in ("gdp", "final_demand", "current_account"):
            for left, right in zip(dense[key], sparse[key]):
                self.assertAlmostEqual(left, right, places=8)

    def test_world_table_object_is_accepted(self):
        import scipy.sparse as sp

        document = synthetic_document()
        table = SimpleNamespace(
            countries=tuple(document["countries"]), industries=tuple(document["industries"]),
            Z=sp.csr_matrix(np.asarray(document["Z"], dtype=float)),
            y=np.asarray(document["y"]), x=np.asarray(document["x"]),
            va=np.asarray(document["va"]), units="USD_mn", tolerance=1e-6,
            final_demand_adjustments=None, va_adjustments=None)
        # WorldTable exposes fda/vaa properties; emulate them
        table.fda = np.zeros(6)
        table.vaa = np.zeros(6)
        flows = sfc_io.country_flows(table)
        reference = sfc_io.country_flows(document)
        for key in ("gdp", "final_demand", "current_account"):
            for left, right in zip(flows[key], reference[key]):
                self.assertAlmostEqual(left, right, places=8)

    def test_signed_adjustments_are_included(self):
        fda = [0.0, 1.0, -0.5, 0.0, 2.0, 0.0]
        vaa = [1.0, 0.0, 0.0, -0.5, 0.0, 2.0]
        document = synthetic_document(adjustments={"fda": fda, "vaa": vaa})
        flows = sfc_io.country_flows(document)
        self.assertAlmostEqual(math.fsum(flows["gdp"]), math.fsum(flows["final_demand"]),
                               places=8)
        self.assertAlmostEqual(math.fsum(flows["current_account"]), 0.0, places=8)
        # GDP includes both value added and its signed adjustment
        Z = np.asarray(document["Z"])
        expected_gdp = np.asarray(document["va"]).reshape(3, 2).sum(axis=1) + \
            np.asarray(vaa).reshape(3, 2).sum(axis=1)
        for expected, value in zip(expected_gdp, flows["gdp"]):
            self.assertAlmostEqual(expected, value, places=8)

    def test_inconsistent_table_fails_closed(self):
        document = synthetic_document()
        document = dict(document)
        document["va"] = [value + 1.0 for value in document["va"]]
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.country_flows(document)
        # malformed shapes / values
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.country_flows({"countries": ["US"], "industries": ["A"]})
        broken = synthetic_document()
        broken["Z"] = [[-1.0] * 6 for _ in range(6)]
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.country_flows(broken)


# ------------------------------------------------------------------------------------- accounts
class SavingsInvestment(unittest.TestCase):
    def test_saving_investment_current_account_identity(self):
        accounts = small_accounts()
        for index in range(3):
            self.assertAlmostEqual(
                accounts["savings"][index] - accounts["investment"][index],
                accounts["current_account"][index], places=8)
        self.assertLess(accounts["checks"]["saving_investment_identity_max_abs"], 1e-9)
        self.assertAlmostEqual(math.fsum(accounts["savings"]),
                               math.fsum(accounts["investment"]), places=8)
        # default split is pure consumption: S = GDP - A = CA
        for savings, ca in zip(accounts["savings"], accounts["current_account"]):
            self.assertAlmostEqual(savings, ca, places=8)

    def test_investment_share_changes_magnitudes_not_the_identity(self):
        default = small_accounts()
        invested = sfc_io.country_accounts(synthetic_document(), investment_share=0.25)
        for index in range(3):
            self.assertAlmostEqual(
                invested["savings"][index] - invested["investment"][index],
                invested["current_account"][index], places=8)
            self.assertAlmostEqual(invested["current_account"][index],
                                   default["current_account"][index], places=8)
            self.assertAlmostEqual(invested["consumption"][index] + invested["investment"][index],
                                   invested["final_demand"][index], places=8)
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.country_accounts(synthetic_document(), investment_share=1.5)

    def test_explicit_consumption_mapping(self):
        document = synthetic_document()
        flows = sfc_io.country_flows(document)
        consumption = {code: 0.6 * value for code, value in zip(flows["countries"],
                                                               flows["final_demand"])}
        accounts = sfc_io.country_accounts(document, consumption=consumption)
        for index in range(3):
            self.assertAlmostEqual(accounts["consumption"][index],
                                   0.6 * flows["final_demand"][index], places=8)
            self.assertAlmostEqual(
                accounts["savings"][index] - accounts["investment"][index],
                accounts["current_account"][index], places=8)
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.country_accounts(document, consumption={"US": 1e9})
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.country_accounts(document, consumption={"XX": 1.0})


# ------------------------------------------------------------------------------------ period step
class PeriodStep(unittest.TestCase):
    def test_step_from_zero_opening_and_rolls_forward(self):
        accounts = small_accounts()
        first = sfc_io.sfc_step(accounts)
        self.assertTrue(first["pass"])
        for code in accounts["countries"]:
            self.assertAlmostEqual(first["closing"][code], first["transactions"][code], places=8)
        self.assertAlmostEqual(first["totals"]["closing"], 0.0, places=8)
        opening = dict(first["closing"])
        second = sfc_io.sfc_step(accounts, opening=opening)
        self.assertTrue(second["pass"])
        for code in accounts["countries"]:
            self.assertAlmostEqual(second["opening"][code], opening[code], places=8)
            self.assertAlmostEqual(
                second["closing"][code],
                opening[code] + accounts["current_account"][
                    accounts["countries"].index(code)], places=8)

    def test_revaluation_step_is_signed_and_must_net_out(self):
        accounts = small_accounts()
        revaluation = {"US": 2.0, "DE": -2.0, "CN": 0.0}
        step = sfc_io.sfc_step(accounts, opening={"US": 5.0, "DE": -5.0, "CN": 0.0},
                               revaluation=revaluation)
        self.assertTrue(step["pass"])
        self.assertAlmostEqual(step["totals"]["revaluation"], 0.0, places=9)
        index = accounts["countries"].index("US")
        self.assertAlmostEqual(
            step["closing"]["US"],
            5.0 + accounts["current_account"][index] + 2.0, places=8)
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.sfc_step(accounts, revaluation={"US": 3.0, "DE": -2.0, "CN": 0.0})
        with self.assertRaises(sfc_io.SFCIOError):
            sfc_io.sfc_step(accounts, opening={"US": 3.0, "DE": 0.0, "CN": 0.0})

    def test_section_is_compact_json_and_reports_checks(self):
        section = sfc_io.sfc_section(synthetic_document(), investment_share=0.2,
                                     include_countries=True)
        payload = json.dumps(section)
        self.assertGreater(len(payload), 100)
        self.assertTrue(section["pass"])
        self.assertEqual(section["countries"], 3)
        self.assertIn("assumptions", section)
        self.assertIn("checks", section)
        totals = section["totals"]
        self.assertAlmostEqual(totals["gdp"], totals["final_demand"], places=8)
        for row in section["accounts"].values():
            self.assertIn("savings", row)
            self.assertIn("current_account", row)
        again = sfc_io.sfc_section(synthetic_document(), investment_share=0.2,
                                   include_countries=True)
        self.assertEqual(payload, json.dumps(again))


# -------------------------------------------------------------------------------------- pipeline
class PipelineIntegration(unittest.TestCase):
    def _args(self, sfc: bool):
        argv = ["--synthetic", "--countries", "4", "--industries", "3", "--no-p2-gate",
                "--out", str(ROOT / "build" / "test_sfc_io")]
        if sfc:
            argv.append("--sfc")
        parser = world_pipeline.build_parser()
        return parser.parse_args(argv)

    def test_flag_is_optional_and_adds_the_block(self):
        parser = world_pipeline.build_parser()
        self.assertFalse(parser.parse_args(["--synthetic"]).sfc)
        self.assertTrue(parser.parse_args(["--synthetic", "--sfc"]).sfc)
        report = world_pipeline.run_pipeline(self._args(sfc=True))
        self.assertIn("sfc", report)
        section = report["sfc"]
        self.assertTrue(section["pass"])
        self.assertEqual(section["module"], "amfe.world.sfc_io")
        json.dumps(report)
        # backward compatibility: without the flag the report has no SFC block
        plain = world_pipeline.run_pipeline(self._args(sfc=False))
        self.assertNotIn("sfc", plain)
        self.assertEqual(plain["status"], world_pipeline.STATUS_OK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
