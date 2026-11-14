"""Static IO/SFC core (P2): 3x3 and 10x30 datasets, productiveness, aggregation, shocks, SFC.

The tests build synthetic tables that satisfy the accounting identities exactly (``y = (I-A)x``,
``Z = A diag(x)``, ``va = x - Z^T 1``), so any failure is a defect of the core rather than of the
fixture.  No network, no external data, deterministic.
"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.world.io_core import (  # noqa: E402
    IOCoreError,
    IODataset,
    LeontiefCore,
    aggregate_matrix_by_country,
    aggregate_matrix_by_industry,
    aggregate_vector_by_country,
    aggregate_vector_by_industry,
    final_demand_shock,
    technology_shock,
)
from amfe.world.sfc_core import SFCFrame, SFCError  # noqa: E402


# --------------------------------------------------------------------------------------- fixtures
def make_dataset(countries, industries, A, x, units="USD_mn") -> IODataset:
    """A table that satisfies both IO identities exactly: y = (I-A)x, Z = A diag(x), va = x - Z^T 1."""
    matrix = np.asarray(A, dtype=float)
    output = np.asarray(x, dtype=float)
    n = output.size
    assert matrix.shape == (n, n)
    intermediate = matrix * output[np.newaxis, :]
    demand = (np.eye(n) - matrix) @ output
    value_added = output - intermediate.sum(axis=0)
    return IODataset.from_dict({
        "countries": list(countries), "industries": list(industries),
        "Z": intermediate.tolist(), "y": demand.tolist(), "x": output.tolist(),
        "va": value_added.tolist(), "units": units,
    })


def three_by_three(units="USD_mn") -> IODataset:
    """3 countries x 3 industries (9 sectors): own-industry 0.15, cross 0.02, productive."""
    nc, ni = 3, 3
    n = nc * ni
    A = np.zeros((n, n))
    for column in range(n):
        A[column, column] = 0.15
        for row in range(n):
            if row != column:
                A[row, column] = 0.02
    x = np.array([100.0, 120.0, 80.0, 110.0, 90.0, 130.0, 95.0, 105.0, 115.0])
    return make_dataset(["US", "EU", "CN"], ["AGRI", "MANU", "SERV"], A, x, units=units)


def ten_by_thirty() -> IODataset:
    """10 countries x 30 industries (300 sectors), column sums below 0.4 so rho(A) < 1."""
    nc, ni = 10, 30
    n = nc * ni
    A = np.empty((n, n))
    for column in range(n):
        for row in range(n):
            jitter = 1.0 + 0.2 * (((row * 7 + column * 3) % 5) / 4.0)
            A[row, column] = (0.30 / n) * jitter
    x = np.array([1000.0 + 10.0 * index for index in range(n)])
    return make_dataset([f"C{country:02d}" for country in range(nc)],
                        [f"I{industry:02d}" for industry in range(ni)], A, x)


def sfc_snapshot():
    """Five accounts in two countries; opening claims, transactions and revaluation."""
    accounts = ["US_HH", "US_FIRM", "US_GOV", "EU_HH", "EU_FIRM"]
    countries = ["US", "US", "US", "EU", "EU"]
    opening = [[0.0, 20.0, 5.0, 0.0, 0.0],
               [0.0, 0.0, 0.0, 10.0, 0.0],
               [0.0, 0.0, 0.0, 0.0, 0.0],
               [0.0, 0.0, 0.0, 0.0, 0.0],
               [0.0, 0.0, 0.0, 0.0, 0.0]]
    transactions = [[0.0, 2.0, 0.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0, 0.0, 0.0]]
    revaluation = [[0.0] * 5 for _ in range(5)]
    revaluation[1][3] = -3.0
    return accounts, countries, opening, transactions, revaluation


def sfc_frame() -> SFCFrame:
    accounts, countries, opening, transactions, revaluation = sfc_snapshot()
    return SFCFrame.from_flows(accounts=accounts, account_countries=countries,
                               opening=opening, transactions=transactions,
                               revaluation=revaluation)


# ------------------------------------------------------------------------------------ IO dataset
class DatasetBalance(unittest.TestCase):
    def test_three_by_three_balance_and_labels(self):
        dataset = three_by_three()
        self.assertEqual(dataset.n_countries, 3)
        self.assertEqual(dataset.n_industries, 3)
        self.assertEqual(dataset.size, 9)
        self.assertEqual(dataset.index("EU", "MANU"), 4)
        self.assertEqual(dataset.country_of(4), "EU")
        self.assertEqual(dataset.industry_of(4), "MANU")
        self.assertEqual(dataset.labels[0], "US/AGRI")
        # the fixture satisfies both identities by construction
        self.assertLess(dataset.balance_residual()["max_abs"], 1e-9)
        self.assertLess(dataset.check_balance(), 1e-9)
        summary = dataset.summarize()
        self.assertEqual(summary["sectors"], 9)
        self.assertAlmostEqual(summary["total_gross_output"],
                               math.fsum(dataset.x), places=9)
        self.assertAlmostEqual(summary["total_final_demand"], math.fsum(dataset.y), places=9)
        self.assertAlmostEqual(summary["total_value_added"], math.fsum(dataset.va), places=9)

    def test_technical_matrix_is_flow_over_output(self):
        dataset = three_by_three()
        A = np.asarray(dataset.technical_matrix(), dtype=float)
        Z = np.asarray(dataset.Z, dtype=float)
        x = np.asarray(dataset.x, dtype=float)
        self.assertTrue(np.all(A >= 0.0))
        for row in range(dataset.size):
            for column in range(dataset.size):
                self.assertAlmostEqual(A[row, column], Z[row, column] / x[column], places=12)
        # by construction Z = A diag(x) with the same A the fixture used
        self.assertAlmostEqual(A[0, 0], 0.15, places=12)
        self.assertAlmostEqual(A[0, 1], 0.02, places=12)

    def test_dataset_validation_is_fail_closed(self):
        base = three_by_three().to_dict()
        # x <= 0 would make A = Z/x meaningless
        broken = dict(base, x=list(base["x"]))
        broken["x"][0] = 0.0
        with self.assertRaises(IOCoreError):
            IODataset.from_dict(broken)
        # a violated product balance must not be silently rebalanced
        broken = dict(base, Z=[list(row) for row in base["Z"]])
        broken["Z"][0][0] += 5.0
        with self.assertRaises(IOCoreError):
            IODataset.from_dict(broken)
        # a violated value-added balance must be rejected too
        broken = dict(base, va=list(base["va"]))
        broken["va"][1] += 1.0
        with self.assertRaises(IOCoreError):
            IODataset.from_dict(broken)
        # negative intermediate flows are not flows
        broken = dict(base, Z=[list(row) for row in base["Z"]])
        broken["Z"][0][0] = -1.0
        with self.assertRaises(IOCoreError):
            IODataset.from_dict(broken)
        # shape mismatch and missing keys
        with self.assertRaises(IOCoreError):
            IODataset.from_dict(dict(base, y=list(base["y"])[:-1]))
        with self.assertRaises(IOCoreError):
            IODataset.from_dict({"countries": ["US"]})


# ------------------------------------------------------------------------------- Leontief algebra
class LeontiefAlgebra(unittest.TestCase):
    def test_inverse_and_solution_reproduce_the_dataset(self):
        dataset = three_by_three()
        core = dataset.leontief()
        inverse = np.asarray(core.inverse(), dtype=float)
        leontief = np.eye(dataset.size) - np.asarray(dataset.technical_matrix(), dtype=float)
        self.assertLess(float(np.max(np.abs(leontief @ inverse - np.eye(dataset.size)))), 1e-9)
        self.assertLess(core.matrix_inverse_product(), 1e-9)
        reproduced = core.gross_output(dataset.y)
        for expected, actual in zip(dataset.x, reproduced):
            self.assertAlmostEqual(expected, actual, places=7)
        multipliers = core.output_multipliers()
        self.assertTrue(all(value > 1.0 for value in multipliers), multipliers)
        self.assertTrue(np.all(np.asarray(core.inverse()) >= -1e-12))

    def test_spectral_radius_and_productiveness(self):
        dataset = three_by_three()
        core = dataset.leontief()
        radius = core.spectral_radius()
        self.assertLess(radius, 1.0)
        self.assertAlmostEqual(radius, 0.31, places=9)
        self.assertTrue(core.is_productive())
        self.assertAlmostEqual(core.productiveness_margin(), 1.0 - radius, places=12)
        uniform = LeontiefCore(tuple(tuple([0.5] * 4) for _ in range(4)),
                              ("a", "b", "c", "d"))
        self.assertAlmostEqual(uniform.spectral_radius(), 2.0, places=9)

    def test_hawkins_simon_condition(self):
        dataset = three_by_three()
        core = dataset.leontief()
        hawkins = core.hawkins_simon()
        self.assertTrue(hawkins["pass"])
        self.assertTrue(hawkins["pivots_positive"])
        self.assertEqual(hawkins["n_minors"], core.size)
        minors = hawkins["leading_principal_minors"]
        self.assertTrue(minors is not None and len(minors) == core.size)
        self.assertTrue(all(minor > 0.0 for minor in minors))
        M = np.eye(core.size) - np.asarray(dataset.technical_matrix(), dtype=float)
        for order, minor in enumerate(minors, start=1):
            self.assertAlmostEqual(minor, float(np.linalg.det(M[:order, :order])), places=9)
        # a non-productive technology fails Hawkins-Simon
        bad = LeontiefCore(((0.8, 0.9), (0.9, 0.8)), ("a", "b"))
        self.assertFalse(bad.is_productive())
        self.assertFalse(bad.hawkins_simon()["pass"])
        self.assertLessEqual(min(bad.hawkins_simon()["leading_principal_minors"]), 0.0)

    def test_hawkins_simon_agrees_with_radius_on_random_matrices(self):
        rng = np.random.default_rng(20261111)
        for scale in (0.05, 0.2, 1.5, 3.0):
            raw = rng.random((6, 6)) * scale
            core = LeontiefCore(tuple(tuple(row) for row in raw),
                                tuple(f"s{index}" for index in range(6)))
            self.assertEqual(core.is_productive(), core.hawkins_simon()["pass"],
                             f"scale={scale} rho={core.spectral_radius()}")

    def test_non_productive_and_singular_are_refused(self):
        singular = LeontiefCore(((1.0, 0.0), (0.0, 0.5)), ("a", "b"))
        self.assertFalse(singular.is_productive())
        with self.assertRaises(IOCoreError):
            singular.inverse()
        with self.assertRaises(IOCoreError):
            singular.gross_output([1.0, 1.0])
        exploding = LeontiefCore(((0.5, 0.6), (0.6, 0.5)), ("a", "b"))
        self.assertGreater(exploding.spectral_radius(), 1.0)
        with self.assertRaises(IOCoreError):
            exploding.gross_output([1.0, 1.0])
        # a non-productive technology cannot be turned into a valid table with positive y
        with self.assertRaises(IOCoreError):
            make_dataset(["US"], ["X"], [[1.2]], [10.0])

    def test_iteration_converges_to_the_direct_solution(self):
        dataset = three_by_three()
        core = dataset.leontief()
        direct = core.gross_output(dataset.y)
        iterative = core.iterate(dataset.y, max_iterations=1000, tolerance=1e-13)
        self.assertTrue(iterative["converged"])
        self.assertLess(iterative["iterations"], 1000)
        for expected, actual in zip(direct, iterative["x"]):
            self.assertAlmostEqual(expected, actual, places=7)
        divergent = LeontiefCore(((0.5, 0.6), (0.6, 0.5)), ("a", "b"))
        self.assertFalse(divergent.iterate([1.0, 1.0], max_iterations=50)["converged"])

    def test_value_added_and_material_residual(self):
        dataset = three_by_three()
        core = dataset.leontief()
        added = core.value_added(dataset.x)
        for expected, actual in zip(dataset.va, added):
            self.assertAlmostEqual(expected, actual, places=7)
        residual = core.material_residual(dataset.x, dataset.y)
        self.assertLess(max(abs(value) for value in residual), 1e-9)
        # negative value added is refused: A^T x exceeds x (only possible for a non-productive A)
        with self.assertRaises(IOCoreError):
            LeontiefCore(((0.9, 0.9), (0.9, 0.9)), ("a", "b")).value_added([1.0, 1.0])


# ------------------------------------------------------------------------------------ aggregation
class Aggregation(unittest.TestCase):
    def test_country_and_industry_aggregation_preserve_totals(self):
        dataset = three_by_three()
        by_country = dataset.aggregate("country")
        self.assertEqual(by_country.countries, ("US", "EU", "CN"))
        self.assertEqual(by_country.industries, ("TOTAL",))
        self.assertEqual(by_country.size, 3)
        self.assertAlmostEqual(math.fsum(by_country.x), math.fsum(dataset.x), places=7)
        self.assertAlmostEqual(math.fsum(by_country.y), math.fsum(dataset.y), places=7)
        self.assertAlmostEqual(math.fsum(by_country.va), math.fsum(dataset.va), places=7)
        self.assertAlmostEqual(by_country.check_balance(), 0.0, places=7)
        self.assertTrue(by_country.leontief().is_productive())
        expected = aggregate_matrix_by_country(dataset, dataset.Z)
        for row in range(3):
            for column in range(3):
                self.assertAlmostEqual(expected[row][column], by_country.Z[row][column], places=9)
        self.assertEqual(aggregate_vector_by_country(dataset, dataset.y), list(by_country.y))

        by_industry = dataset.aggregate("industry")
        self.assertEqual(by_industry.countries, ("WORLD",))
        self.assertEqual(by_industry.industries, dataset.industries)
        self.assertEqual(by_industry.size, 3)
        self.assertAlmostEqual(math.fsum(by_industry.x), math.fsum(dataset.x), places=7)
        self.assertAlmostEqual(by_industry.check_balance(), 0.0, places=7)
        expected_i = aggregate_matrix_by_industry(dataset, dataset.Z)
        for row in range(3):
            for column in range(3):
                self.assertAlmostEqual(expected_i[row][column], by_industry.Z[row][column], places=9)
        self.assertEqual(aggregate_vector_by_industry(dataset, dataset.x), list(by_industry.x))
        with self.assertRaises(IOCoreError):
            dataset.aggregate("continent")


# --------------------------------------------------------------------------------------- scenarios
class Scenarios(unittest.TestCase):
    def test_final_demand_shock_matches_the_algebra(self):
        A = np.array([[0.2, 0.1], [0.3, 0.1]])
        x = np.array([100.0, 50.0])
        dataset = make_dataset(["US"], ["X", "Y"], A, x)
        delta_y = [10.0, 0.0]
        result = final_demand_shock(dataset, delta_y)
        inverse = np.linalg.inv(np.eye(2) - A)
        expected_x = inverse @ (np.asarray(dataset.y) + np.asarray(delta_y))
        expected_delta = expected_x - x
        for expected, actual in zip(expected_x, result["result"]["gross_output"]):
            self.assertAlmostEqual(expected, actual, places=7)
        for expected, actual in zip(expected_delta, result["result"]["delta_gross_output"]):
            self.assertAlmostEqual(expected, actual, places=7)
        self.assertAlmostEqual(result["shock"]["total_delta_final_demand"], 10.0, places=9)
        self.assertAlmostEqual(result["result"]["total_delta_gross_output"],
                               float(expected_delta.sum()), places=7)
        self.assertAlmostEqual(result["result"]["output_multiplier"],
                               float(expected_delta.sum()) / 10.0, places=7)
        self.assertLess(result["balances"]["material_residual_max_abs"], 1e-9)
        # negative demand, or a shock producing negative output, is refused
        with self.assertRaises(IOCoreError):
            final_demand_shock(dataset, [-(dataset.y[0] + 1.0), 0.0])
        with self.assertRaises(IOCoreError):
            final_demand_shock(dataset, [-1e9, 0.0])

    def test_technology_shock_numbers_and_fail_closed(self):
        A = np.array([[0.2, 0.1], [0.3, 0.1]])
        x = np.array([100.0, 50.0])
        dataset = make_dataset(["US"], ["X", "Y"], A, x)
        delta_A = [[0.0, 0.0], [0.1, 0.0]]
        result = technology_shock(dataset, delta_A)
        A_new = A + np.asarray(delta_A)
        expected = np.linalg.inv(np.eye(2) - A_new) @ np.asarray(dataset.y)
        for value, actual in zip(expected, result["result"]["gross_output"]):
            self.assertAlmostEqual(value, actual, places=7)
        self.assertAlmostEqual(result["technology"]["max_abs_delta_A"], 0.1, places=12)
        self.assertTrue(result["technology"]["productive_after"])
        self.assertGreater(result["result"]["total_gross_output"],
                           math.fsum(dataset.x))
        # a technology shock that destroys productiveness is refused
        with self.assertRaises(IOCoreError):
            technology_shock(dataset, [[0.8, 0.0], [0.0, 0.8]])
        # a shock that makes a coefficient negative is refused
        with self.assertRaises(IOCoreError):
            technology_shock(dataset, [[-0.5, 0.0], [0.0, 0.0]])


# ------------------------------------------------------------------------------------------- scale
class Scale(unittest.TestCase):
    def test_ten_countries_thirty_industries(self):
        dataset = ten_by_thirty()
        self.assertEqual((dataset.n_countries, dataset.n_industries, dataset.size), (10, 30, 300))
        self.assertLess(dataset.check_balance(), 1e-8)
        core = dataset.leontief()
        self.assertTrue(core.is_productive())
        self.assertLess(core.spectral_radius(), 1.0)
        self.assertTrue(core.hawkins_simon()["pass"])
        self.assertLess(core.matrix_inverse_product(), 1e-9)
        reproduced = core.gross_output(dataset.y)
        self.assertLess(max(abs(a - b) for a, b in zip(dataset.x, reproduced)), 1e-6)
        multipliers = core.output_multipliers()
        self.assertEqual(len(multipliers), 300)
        self.assertTrue(all(math.isfinite(value) and value > 1.0 for value in multipliers))
        by_country = dataset.aggregate("country")
        self.assertEqual(by_country.size, 10)
        self.assertAlmostEqual(math.fsum(by_country.x), math.fsum(dataset.x), places=4)


# -------------------------------------------------------------------- determinism and immutability
class Determinism(unittest.TestCase):
    def test_repeated_calls_are_identical_and_inputs_are_copied(self):
        dataset = three_by_three()
        first = dataset.leontief().describe()
        second = three_by_three().leontief().describe()
        self.assertEqual(first, second)
        shock_one = final_demand_shock(dataset, [5.0] * dataset.size)
        shock_two = final_demand_shock(dataset, [5.0] * dataset.size)
        self.assertEqual(shock_one, shock_two)
        # mutating the dict after construction must not change the frozen dataset
        payload = three_by_three().to_dict()
        frozen = IODataset.from_dict(payload)
        payload["Z"][0][0] = 1e6
        payload["x"][0] = 1e6
        self.assertEqual(frozen, three_by_three())
        with self.assertRaises(Exception):
            frozen.x = (1.0,) * frozen.size  # frozen dataclass


# --------------------------------------------------------------------------------------------- SFC
class SFCFrameInvariants(unittest.TestCase):
    def test_four_phase_invariants(self):
        frame = sfc_frame()
        self.assertEqual(frame.size, 5)
        self.assertEqual(frame.countries, ("EU", "US"))
        self.assertLess(frame.closing_identity_residual(), 1e-12)
        consistency = frame.consistency()
        self.assertTrue(consistency["pass"])
        for phase in ("opening", "transactions", "revaluation", "closing"):
            self.assertLess(frame.row_column_residual(phase), 1e-12)
            self.assertLess(abs(frame.total_net_worth(phase)), 1e-12)
            self.assertLess(frame.external_residual(phase), 1e-12)
        self.assertLess(frame.net_worth_residual(), 1e-12)
        # closing equals opening + transactions + revaluation elementwise
        for row in range(frame.size):
            for column in range(frame.size):
                self.assertAlmostEqual(
                    frame.closing[row][column],
                    frame.opening[row][column] + frame.transactions[row][column]
                    + frame.revaluation[row][column], places=12)
        net_worths = frame.net_worths("closing")
        self.assertAlmostEqual(net_worths["US_HH"], 27.0, places=12)
        self.assertAlmostEqual(net_worths["US_FIRM"], -14.0, places=12)
        self.assertAlmostEqual(net_worths["US_GOV"], -5.0, places=12)
        self.assertAlmostEqual(net_worths["EU_HH"], -8.0, places=12)
        self.assertAlmostEqual(math.fsum(net_worths.values()), 0.0, places=12)
        # net worth reconciles with net lending and revaluation gains
        self.assertAlmostEqual(net_worths["US_HH"],
                               frame.net_worths("opening")["US_HH"] + 2.0, places=12)

    def test_cross_border_positions_net_out(self):
        frame = sfc_frame()
        positions = frame.external_positions("closing")
        self.assertAlmostEqual(positions["US"], 8.0, places=12)   # 10 + 1 transaction - 3 revaluation
        self.assertAlmostEqual(positions["EU"], -8.0, places=12)
        self.assertAlmostEqual(math.fsum(positions.values()), 0.0, places=12)
        self.assertAlmostEqual(frame.bilateral_position("closing", "US", "EU"), 8.0, places=12)
        self.assertAlmostEqual(frame.bilateral_position("closing", "EU", "US"), -8.0, places=12)
        book = frame.country_balance_sheet("closing")
        self.assertAlmostEqual(book["US"]["net_external_position"], 8.0, places=12)
        # flows across countries also net out (no cross-border flow in the fixture)
        self.assertAlmostEqual(frame.external_residual("transactions"), 0.0, places=12)
        self.assertAlmostEqual(frame.country_net_worths("closing")["US"],
                               27.0 - 14.0 - 5.0, places=12)

    def test_period_step_rolls_opening_into_closing(self):
        frame = sfc_frame()
        moves = [[0.0] * 5 for _ in range(5)]
        moves[0][1] = 4.0
        revaluation = [[0.0] * 5 for _ in range(5)]
        revaluation[3][0] = 2.0
        second = frame.step_period(moves, revaluation)
        for row in range(5):
            for column in range(5):
                self.assertAlmostEqual(second.opening[row][column], frame.closing[row][column],
                                       places=12)
                self.assertAlmostEqual(
                    second.closing[row][column],
                    frame.closing[row][column] + moves[row][column] + revaluation[row][column],
                    places=12)
        self.assertTrue(second.consistency()["pass"])
        self.assertLess(second.net_worth_residual(), 1e-12)
        third = second.step_period([[0.0] * 5 for _ in range(5)], [[0.0] * 5 for _ in range(5)])
        self.assertTrue(third.consistency()["pass"])
        self.assertAlmostEqual(third.total_net_worth("closing"), 0.0, places=12)

    def test_fail_closed_validation(self):
        accounts, countries, opening, transactions, revaluation = sfc_snapshot()
        closing = [[opening[row][column] + transactions[row][column] + revaluation[row][column]
                    for column in range(5)] for row in range(5)]
        closing[0][1] += 1.0  # breaks closing = opening + transactions + revaluation
        with self.assertRaises(SFCError):
            SFCFrame(accounts=accounts, account_countries=countries, opening=opening,
                     transactions=transactions, revaluation=revaluation, closing=closing)
        # negative claims are not claims
        bad = [list(row) for row in opening]
        bad[0][1] = -1.0
        with self.assertRaises(SFCError):
            SFCFrame.from_flows(accounts=accounts, account_countries=countries, opening=bad,
                                transactions=transactions, revaluation=revaluation)
        # a non-square matrix is rejected
        with self.assertRaises(SFCError):
            SFCFrame.from_flows(accounts=accounts, account_countries=countries,
                                opening=opening[:-1], transactions=transactions,
                                revaluation=revaluation)
        # one country label per account is required
        with self.assertRaises(SFCError):
            SFCFrame.from_flows(accounts=accounts, account_countries=countries[:-1],
                                opening=opening, transactions=transactions,
                                revaluation=revaluation)

    def test_determinism(self):
        first = sfc_frame().describe()
        second = sfc_frame().describe()
        self.assertEqual(first, second)
        self.assertTrue(first["consistent"])
        self.assertAlmostEqual(first["net_worth_total"]["closing"], 0.0, places=9)
        frame = sfc_frame()
        with self.assertRaises(Exception):
            frame.closing = ((0.0,) * 5,) * 5  # frozen dataclass


if __name__ == "__main__":
    unittest.main(verbosity=2)
