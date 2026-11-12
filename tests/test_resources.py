"""Tests for the AMFE stage-4 real-economy and resources module (``src/amfe/resources``).

The suite is offline and uses the standard library only (the CI runner installs no numpy), so it
can be executed exactly as the other regressions: ``python tests/test_resources.py``.

Coverage:
* units, dimensions and the POTENTIAL/ACCESSIBLE/UTILIZED ordering (docs/37, docs/38 §3);
* Leontief inverse, productiveness check, gross output, value added and the no-double-counting
  identity (docs/38 I01, E01);
* logistics: transport delays, inventories, capacities, losses, shock propagation and both
  conservation identities (docs/38 I04, block 08);
* energy/raw-material balance and the physical output limit (docs/38 block 09, E03);
* labour endowments and the B0/B1/B2 variants compared on independent parameters, including the
  automation case (docs/37 R03/R04/R05, no labour theory of value);
* material balances, trade hooks for stage 3 and the combined shock scenarios.
"""
from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amfe.resources import (  # noqa: E402
    AggregateLabourConstraint,
    BalanceError,
    DimensionMismatch,
    InputConstraint,
    LabourError,
    LabourStates,
    LeontiefError,
    LeontiefModel,
    LogisticsEdge,
    LogisticsError,
    LogisticsNode,
    MaterialBalances,
    NoLabourConstraint,
    ProductBalance,
    ResourceBalance,
    ResourceBalanceError,
    ResourceError,
    ResourceQuantity,
    ResourceState,
    Scenario,
    ScenarioError,
    StateOrderError,
    SupplyChain,
    TaskLabourConstraint,
    Unit,
    UnitRegistry,
    apply_input_limit,
    balances_from_io,
    chain_topology,
    check_no_double_counting,
    check_state_order,
    compare_labour_variants,
    compare_scenarios,
    employment_from_hours,
    energy_balance_from_io,
    run_scenario,
    standard_shock_set,
    supply_shock,
    unemployment_rate,
)

TOL = 1e-9
#: Two-sector technology used throughout: a uses 0.2 of itself and 0.3 of b per unit, etc.
TWO = LeontiefModel(((0.2, 0.3), (0.1, 0.4)), ("a", "b"))
TWO_DEMAND = (10.0, 20.0)               # final demand of the reference solution
TWO_OUTPUT = (80.0 / 3.0, 340.0 / 9.0)  # x = (I-A)^-1 d: (26.66.., 37.77..)
#: energy per unit of gross output, MWh; required at TWO_OUTPUT = 2*x_a + 1*x_b = 820/9
TWO_ENERGY_INTENSITY = {"a": 2.0, "b": 1.0}
TWO_ENERGY_REQUIRED = 2.0 * TWO_OUTPUT[0] + TWO_OUTPUT[1]


def close(first, second, tolerance=TOL) -> bool:
    return abs(first - second) <= tolerance


class ResourceUnits(unittest.TestCase):
    def test_convert_within_dimension(self):
        registry = UnitRegistry()
        self.assertTrue(close(registry.convert(1.0, "MWh", "GJ"), 3.6))
        self.assertTrue(close(registry.convert(3.6, "GJ", "MWh"), 1.0))
        self.assertTrue(close(registry.convert(1000.0, "kg", "tonne"), 1.0))
        self.assertEqual(registry.dimension("MWh"), registry.dimension("GJ"))
        self.assertTrue(registry.same_dimension("hour", "person") is False)

    def test_convert_refuses_other_dimensions_and_unknown_units(self):
        registry = UnitRegistry()
        with self.assertRaises(DimensionMismatch):
            registry.convert(1.0, "MWh", "tonne")
        with self.assertRaises(ResourceError):
            registry.convert(1.0, "MWh", "bushel")
        with self.assertRaises(ResourceError):
            Unit("bad", "energy", 0.0)

    def test_state_order_valid_and_invalid(self):
        potential = ResourceQuantity("electricity", 100.0, "MWh", ResourceState.POTENTIAL)
        accessible = ResourceQuantity("electricity", 80.0, "MWh", ResourceState.ACCESSIBLE)
        utilized = ResourceQuantity("electricity", 60.0, "MWh", ResourceState.UTILIZED)
        check_state_order(potential, accessible, utilized)  # no exception
        # the same quantity in another unit of the same dimension: convert before comparing
        check_state_order(potential, ResourceQuantity("electricity", 80.0 * 3.6, "GJ",
                                                      ResourceState.ACCESSIBLE),
                          utilized, registry=UnitRegistry())
        with self.assertRaises(StateOrderError):
            check_state_order(potential, accessible,
                              ResourceQuantity("electricity", 90.0, "MWh", ResourceState.UTILIZED))
        with self.assertRaises(StateOrderError):
            check_state_order(potential,
                              ResourceQuantity("electricity", 120.0, "MWh", ResourceState.ACCESSIBLE),
                              utilized)
        with self.assertRaises(StateOrderError):
            # a different horizon is a different quantity: the ordering is meaningless
            check_state_order(potential,
                              ResourceQuantity("electricity", 80.0, "MWh",
                                               ResourceState.ACCESSIBLE, horizon="2030"),
                              utilized)
        with self.assertRaises(StateOrderError):
            check_state_order(potential, accessible,
                              ResourceQuantity("labour", 60.0, "MWh", ResourceState.UTILIZED))
        with self.assertRaises(ResourceError):
            ResourceQuantity("electricity", -1.0, "MWh", ResourceState.UTILIZED)


class InputOutput(unittest.TestCase):
    def test_inverse_satisfies_identity(self):
        inverse = TWO.inverse()
        expected = ((4.0 / 3.0, 2.0 / 3.0), (2.0 / 9.0, 16.0 / 9.0))
        for row in range(2):
            for column in range(2):
                self.assertTrue(close(inverse[row][column], expected[row][column], 1e-12))
        leontief = TWO.leontief_matrix()
        for row in range(2):
            for column in range(2):
                value = sum(leontief[row][k] * inverse[k][column] for k in range(2))
                self.assertTrue(close(value, 1.0 if row == column else 0.0, 1e-12))

    def test_gross_output_and_material_balance(self):
        output = TWO.gross_output(TWO_DEMAND)
        for index in range(2):
            self.assertTrue(close(output[index], TWO_OUTPUT[index], 1e-12))
        self.assertLessEqual(TWO.check_material_balance(output, TWO_DEMAND), 1e-12)
        # linearity: a uniform scaling of final demand scales the solution
        doubled = TWO.gross_output([2.0 * value for value in TWO_DEMAND])
        for index in range(2):
            self.assertTrue(close(doubled[index], 2.0 * output[index], 1e-12))
        reconstructed = TWO.final_demand_from_output(output)
        self.assertTrue(close(reconstructed[0], TWO_DEMAND[0], 1e-12))
        self.assertTrue(close(reconstructed[1], TWO_DEMAND[1], 1e-12))

    def test_productiveness_is_checked_not_assumed(self):
        self.assertGreater(TWO.spectral_radius(), 0.49)
        self.assertLess(TWO.spectral_radius(), 0.51)
        self.assertTrue(TWO.is_productive())
        self.assertGreater(TWO.productiveness_margin(), 0.4)
        non_productive = LeontiefModel.uniform(["x", "y", "z"], 0.4)  # rho = 1.2
        self.assertFalse(non_productive.is_productive())
        with self.assertRaises(LeontiefError):
            non_productive.gross_output([1.0, 1.0, 1.0])
        with self.assertRaises(LeontiefError):
            LeontiefModel(((2.0,),), ("a",)).gross_output([1.0])
        with self.assertRaises(LeontiefError):
            LeontiefModel(((1.0,),), ("a",)).inverse()  # singular (I - A)
        # the boundary rho = 1 is not productive either
        boundary = LeontiefModel(((0.0, 1.0), (1.0, 0.0)), ("a", "b"))
        self.assertFalse(boundary.is_productive())

    def test_value_added_equals_final_uses_no_double_counting(self):
        output = TWO.gross_output(TWO_DEMAND)
        added = TWO.value_added(output)
        self.assertTrue(close(added[0], TWO_DEMAND[0], 1e-12))
        self.assertTrue(close(added[1], TWO_DEMAND[1], 1e-12))
        self.assertTrue(close(TWO.total_value_added(output), sum(TWO_DEMAND), 1e-12))
        reconciliation = check_no_double_counting(TWO, output, TWO_DEMAND)
        self.assertTrue(close(reconciliation["residual"], 0.0, 1e-12))
        self.assertGreater(reconciliation["gross_output"], reconciliation["value_added"])
        # gross output and intermediate use are never added to each other
        self.assertGreater(reconciliation["intermediate_use"], 0.0)
        self.assertTrue(close(reconciliation["gross_output"],
                              reconciliation["intermediate_use"] + reconciliation["value_added"]))

    def test_multipliers_and_linkages(self):
        multipliers = TWO.output_multipliers()
        self.assertTrue(close(multipliers[0], 4.0 / 3.0 + 2.0 / 9.0, 1e-12))
        self.assertTrue(close(multipliers[1], 2.0 / 3.0 + 16.0 / 9.0, 1e-12))
        self.assertEqual(multipliers, TWO.backward_linkages())
        forward = TWO.forward_linkages()
        self.assertTrue(close(forward[0], 4.0 / 3.0 + 2.0 / 3.0, 1e-12))
        self.assertTrue(close(forward[1], 2.0 / 9.0 + 16.0 / 9.0, 1e-12))
        for value in multipliers + forward:
            self.assertGreaterEqual(value, 1.0 - 1e-12)  # productive technology

    def test_invalid_io_data(self):
        with self.assertRaises(LeontiefError):
            LeontiefModel(((0.2, -0.1), (0.0, 0.1)), ("a", "b"))
        with self.assertRaises(LeontiefError):
            LeontiefModel(((0.2, 0.3), (0.1, 0.4)), ("a",))
        with self.assertRaises(LeontiefError):
            LeontiefModel(((0.2, 0.3), (0.1, 0.4)), ("a", "a"))
        with self.assertRaises(LeontiefError):
            LeontiefModel(((0.2, 0.3, 0.1), (0.1, 0.4, 0.0)), ("a", "b"))
        with self.assertRaises(LeontiefError):
            TWO.gross_output([-1.0, 20.0])
        with self.assertRaises(LeontiefError):
            TWO.gross_output([10.0])
        with self.assertRaises(LeontiefError):
            TWO.intermediate_use([-1.0, 0.0])
        with self.assertRaises(LeontiefError):
            LeontiefModel.from_requirements({"a": {"a": 0.1}, "b": {"a": 0.1}})  # column b missing
        self.assertEqual(TWO.describe()["productive"], True)

    def test_random_productive_grid(self):
        generator = random.Random(20261112)
        for _ in range(20):
            size = generator.randint(2, 5)
            coefficient = generator.uniform(0.01, 0.9 / size)
            sectors = [f"s{index}" for index in range(size)]
            model = LeontiefModel.uniform(sectors, coefficient)
            self.assertTrue(model.is_productive(), (size, coefficient))
            demand = [generator.uniform(1.0, 100.0) for _ in range(size)]
            output = model.gross_output(demand)
            for value in output:
                self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(model.check_material_balance(output, demand), 1e-9)
            self.assertTrue(close(model.total_value_added(output), math.fsum(demand), 1e-9))


class Logistics(unittest.TestCase):
    def test_transport_delay(self):
        chain = SupplyChain([LogisticsNode("a"), LogisticsNode("b")],
                            [LogisticsEdge("a", "b", capacity=100.0, delay=2)])
        reports = chain.run(4, supplies={1: {"a": 100.0}}, demand={1: {"a": 100.0}})
        self.assertTrue(close(reports[0].arrivals["b"], 0.0))
        self.assertTrue(close(reports[1].arrivals["b"], 0.0))
        self.assertTrue(close(reports[2].arrivals["b"], 100.0))  # dispatched in period 1, delay 2
        self.assertTrue(close(reports[2].inventory["b"], 100.0))
        self.assertEqual(chain.check_node_conservation(), {"a": 0.0, "b": 0.0})
        self.assertTrue(close(chain.check_mass_conservation(), 0.0))

    def test_capacity_inventory_and_unmet_demand(self):
        chain = SupplyChain([LogisticsNode("a"), LogisticsNode("b")],
                            [LogisticsEdge("a", "b", capacity=5.0, delay=1)])
        reports = chain.run(3, supplies={1: {"a": 10.0}}, demand={1: {"a": 10.0}})
        self.assertTrue(close(reports[0].shipped[("a", "b")], 5.0))
        self.assertTrue(close(reports[0].unmet["a"], 5.0))
        self.assertTrue(close(reports[0].inventory["a"], 5.0))
        self.assertTrue(close(reports[1].arrivals["b"], 5.0))
        # the stock that did not fit is still there: no negative inventory anywhere
        for report in reports:
            for value in report.inventory.values():
                self.assertGreaterEqual(value, 0.0)
        self.assertEqual(chain.check_node_conservation(), {"a": 0.0, "b": 0.0})
        self.assertTrue(close(chain.check_mass_conservation(), 0.0))

    def test_losses_are_physical_and_never_negative(self):
        chain = SupplyChain([LogisticsNode("a"), LogisticsNode("b")],
                            [LogisticsEdge("a", "b", capacity=100.0, delay=1, loss=0.1)])
        reports = chain.run(2, supplies={1: {"a": 10.0}}, demand={1: {"a": 10.0}})
        self.assertTrue(close(reports[0].losses[("a", "b")], 1.0))
        self.assertTrue(close(reports[1].arrivals["b"], 9.0))
        self.assertTrue(close(chain.check_mass_conservation(), 0.0))

    def test_supply_shock_propagates_downstream(self):
        chain = chain_topology(
            ["port", "factory", "market"],
            [("port", "factory", 10.0, 1, 0.0), ("factory", "market", 10.0, 1, 0.0)],
        )
        supply = {period: {"port": 10.0} for period in range(1, 9)}
        demand = {period: {"port": 10.0} for period in range(1, 9)}
        for period in range(3, 9):
            demand[period]["factory"] = 10.0

        # baseline: the pipeline is filled and every demand is met
        chain.run(8, supply, demand)
        self.assertTrue(close(chain.unmet_total(), 0.0))
        self.assertEqual(chain.check_node_conservation(), {"port": 0.0, "factory": 0.0,
                                                          "market": 0.0})
        self.assertTrue(close(chain.check_mass_conservation(), 0.0))

        # shock: every edge is closed in periods 3 and 4
        shocked = supply_shock(chain, horizon=8, shock_periods=[3, 4], edge_factor=0.0,
                               supplies=supply, demand=demand)
        self.assertGreater(shocked["unmet_total"], 0.0)
        self.assertTrue(close(shocked["mass_error"], 0.0))
        self.assertEqual(set(shocked["conservation_error"].values()), {0.0})

        # the transport delay puts the blocked dispatches exactly into periods 4 and 5
        chain.reset()
        reports = chain.run(8, supply, demand,
                            {3: {0: 0.0, 1: 0.0}, 4: {0: 0.0, 1: 0.0}})
        self.assertTrue(close(reports[2].arrivals["factory"], 10.0))  # dispatched in period 2
        self.assertTrue(close(reports[3].arrivals["factory"], 0.0))   # dispatched in period 3
        self.assertTrue(close(reports[4].arrivals["factory"], 0.0))   # dispatched in period 4
        self.assertTrue(close(reports[5].arrivals["factory"], 10.0))  # recovery from period 5
        self.assertTrue(close(chain.check_mass_conservation(), 0.0))

    def test_invalid_network_data(self):
        with self.assertRaises(LogisticsError):
            LogisticsEdge("a", "a", 1.0, 1)
        with self.assertRaises(LogisticsError):
            LogisticsEdge("a", "b", -1.0, 1)
        with self.assertRaises(LogisticsError):
            LogisticsEdge("a", "b", 1.0, 0)  # every link takes at least one period
        with self.assertRaises(LogisticsError):
            LogisticsEdge("a", "b", 1.0, 1, loss=1.0)
        with self.assertRaises(LogisticsError):
            SupplyChain([LogisticsNode("a"), LogisticsNode("a")], [])
        with self.assertRaises(LogisticsError):
            SupplyChain([LogisticsNode("a")], [LogisticsEdge("a", "b", 1.0, 1)])
        chain = SupplyChain([LogisticsNode("a")], [])
        with self.assertRaises(LogisticsError):
            chain.step(demand={"unknown": 1.0})
        with self.assertRaises(LogisticsError):
            chain.step(supplies={"unknown": 1.0})


class EnergyAndMaterials(unittest.TestCase):
    def test_energy_balance_identity(self):
        balance = ResourceBalance(
            "electricity", "MWh",
            {"domestic_extraction": 100.0, "imports": 10.0},
            {"intermediate_use": 80.0, "final_consumption": 25.0, "losses": 5.0},
        )
        self.assertTrue(close(balance.residual, 0.0))
        self.assertTrue(close(balance.validate(), 0.0))
        self.assertTrue(close(balance.supply_total, 110.0))
        imbalance = ResourceBalance(
            "electricity", "MWh",
            {"domestic_extraction": 100.0},
            {"intermediate_use": 80.0, "final_consumption": 30.0},
        )
        self.assertFalse(close(imbalance.residual, 0.0))
        with self.assertRaises(ResourceBalanceError):
            imbalance.validate()
        with self.assertRaises(ResourceBalanceError):
            ResourceBalance("electricity", "MWh", {"domestic_extraction": 10.0},
                            {"intermediate_use": -1.0})
        # a stock change must appear exactly once (no double counting, docs/38 I04)
        with self.assertRaises(ResourceBalanceError):
            ResourceBalance("electricity", "MWh",
                            {"domestic_extraction": 10.0, "stock_draw": 5.0},
                            {"intermediate_use": 10.0, "stock_build": 5.0})

    def test_energy_limits_output(self):
        constraint = InputConstraint(dict(TWO_ENERGY_INTENSITY), available=50.0)
        full_output = dict(zip(TWO.sector_ids, TWO.gross_output(TWO_DEMAND)))
        self.assertTrue(close(constraint.required(full_output), TWO_ENERGY_REQUIRED, 1e-12))
        self.assertTrue(constraint.binding(full_output))  # 91.1 > 50: binding
        limited = apply_input_limit(TWO, TWO_DEMAND, constraint)
        self.assertTrue(close(limited.scale, 50.0 / TWO_ENERGY_REQUIRED, 1e-12))
        self.assertTrue(limited.binding)
        self.assertTrue(close(limited.required_at_scaled_output, 50.0, 1e-12))
        self.assertTrue(close(limited.value_added, 30.0 * limited.scale, 1e-12))
        self.assertTrue(close(limited.required_at_full_output, TWO_ENERGY_REQUIRED, 1e-12))
        for sector, value in limited.output.items():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, dict(full_output)[sector] + 1e-12)
        self.assertEqual(limited.describe()["binding"], True)

    def test_energy_not_binding_and_zero_intensity(self):
        ample = InputConstraint(dict(TWO_ENERGY_INTENSITY), available=10_000.0)
        limited = apply_input_limit(TWO, TWO_DEMAND, ample)
        self.assertEqual(limited.scale, 1.0)
        self.assertFalse(limited.binding)
        self.assertTrue(close(limited.value_added, 30.0, 1e-12))
        # a sector with zero energy intensity adds no requirement
        zero = InputConstraint({"a": 0.0, "b": 0.0}, available=0.0)
        self.assertEqual(zero.required({"a": 100.0, "b": 100.0}), 0.0)
        self.assertEqual(zero.feasible_scale({"a": 100.0, "b": 100.0}), 1.0)
        self.assertFalse(zero.binding({"a": 100.0, "b": 100.0}))

    def test_energy_balance_closes_for_an_io_solution(self):
        constraint = InputConstraint(dict(TWO_ENERGY_INTENSITY), available=100.0)
        limited = apply_input_limit(TWO, TWO_DEMAND, constraint)
        balance = energy_balance_from_io(limited, domestic_extraction=100.0)
        self.assertTrue(close(balance.validate(), 0.0))
        self.assertTrue(close(balance.supply_total, 100.0))
        self.assertTrue(close(balance.residual, 0.0))
        with self.assertRaises(ResourceBalanceError):
            energy_balance_from_io(limited, domestic_extraction=1.0)  # supply below use

    def test_product_balance_supply_equals_use(self):
        balance = ProductBalance(
            "steel", "tonne",
            supply={"domestic_output": 10.0, "imports": 2.0},
            use={"intermediate_use": 5.0, "final_consumption": 6.0, "exports": 1.0},
        )
        self.assertTrue(close(balance.validate(), 0.0))
        self.assertTrue(close(balance.supply_total, 12.0))
        # stock changes are part of the supply/use sides, not an extra column
        with_stock = ProductBalance(
            "steel", "tonne",
            supply={"domestic_output": 10.0, "stock_draw": 2.0},
            use={"intermediate_use": 9.0, "final_consumption": 3.0},
        )
        self.assertTrue(close(with_stock.validate(), 0.0))
        with self.assertRaises(BalanceError):
            ProductBalance("steel", "tonne", {"domestic_output": 10.0},
                           {"intermediate_use": 9.0}).validate()
        with self.assertRaises(BalanceError):
            ProductBalance("steel", "tonne", {"domestic_output": -1.0},
                           {"intermediate_use": 1.0})

    def test_material_balances_validate_all_products(self):
        balances = MaterialBalances([
            ProductBalance("steel", "tonne", {"domestic_output": 5.0},
                           {"final_consumption": 5.0}),
            ProductBalance("power", "MWh", {"domestic_output": 3.0},
                           {"intermediate_use": 3.0}),
        ])
        self.assertEqual(balances.validate(), {"steel": 0.0, "power": 0.0})
        with self.assertRaises(BalanceError):
            MaterialBalances([ProductBalance("steel", "tonne", {"domestic_output": 1.0},
                                             {"final_consumption": 1.0}),
                              ProductBalance("steel", "tonne", {"domestic_output": 1.0},
                                             {"final_consumption": 1.0})])

    def test_balances_from_io_closed_and_open(self):
        output = TWO.gross_output(TWO_DEMAND)
        closed = balances_from_io(TWO, output, TWO_DEMAND)
        for product, residual in closed.validate().items():
            self.assertTrue(close(residual, 0.0), (product, residual))
        # stage-3 hook: balanced trade flows keep the identity, an unbalanced vector raises.
        # x = A x + domestic_final + exports - imports, so the IO solve uses the net demand.
        domestic_final = [10.0, 20.0]
        imports = {"a": 1.0, "b": 1.0}
        exports = {"a": 3.0, "b": 1.0}
        trade_solve = [domestic_final[0] + exports["a"] - imports["a"],
                       domestic_final[1] + exports["b"] - imports["b"]]
        output_with_trade = TWO.gross_output(trade_solve)
        trade = balances_from_io(TWO, output_with_trade, domestic_final,
                                 imports=imports, exports=exports)
        for residual in trade.validate().values():
            self.assertTrue(close(residual, 0.0), residual)
        inconsistent = balances_from_io(TWO, output, TWO_DEMAND, imports={"a": 5.0})
        with self.assertRaises(BalanceError):
            inconsistent.validate()


class Labour(unittest.TestCase):
    def test_three_states_and_utilization(self):
        states = LabourStates(
            potential={"manual": 100.0, "cognitive": 50.0},
            accessible={"manual": 80.0, "cognitive": 40.0},
            utilized={"manual": 60.0, "cognitive": 30.0},
        )
        self.assertEqual(states.total("potential"), 150.0)
        self.assertEqual(states.total("utilized"), 90.0)
        self.assertTrue(close(states.utilized_share(), 90.0 / 150.0))
        self.assertEqual(states.group_utilization(), {"manual": 0.75, "cognitive": 0.75})
        self.assertEqual(states.describe()["unit"], "hour")
        with self.assertRaises(LabourError):
            LabourStates({"manual": 10.0}, {"manual": 11.0}, {"manual": 5.0})
        with self.assertRaises(LabourError):
            LabourStates({"manual": 10.0}, {"manual": 9.0}, {"manual": 9.5})
        with self.assertRaises(LabourError):
            LabourStates({"manual": 10.0}, {"other": 1.0}, {"manual": 0.0})  # groups must agree
        with self.assertRaises(LabourError):
            states.total("unknown")

    def test_employment_and_unemployment_conversions(self):
        self.assertTrue(close(employment_from_hours(2000.0, 40.0), 50.0))
        self.assertTrue(close(unemployment_rate(100.0, 95.0), 0.05))
        self.assertEqual(unemployment_rate(0.0, 0.0), 0.0)
        with self.assertRaises(LabourError):
            unemployment_rate(100.0, 101.0)
        with self.assertRaises(LabourError):
            employment_from_hours(10.0, 0.0)

    def test_b0_baseline_has_no_labour_constraint(self):
        output = dict(zip(TWO.sector_ids, TWO.gross_output(TWO_DEMAND)))
        b0 = NoLabourConstraint(descriptive_hours={"a": 10_000.0})
        self.assertIsNone(b0.required_hours(output))
        self.assertEqual(b0.feasible_scale(output), 1.0)
        self.assertIsNone(b0.binding(output))
        self.assertFalse(b0.describe()["labour_in_production"])
        # R04: a fully automated sector produces with zero labour input
        automated = AggregateLabourConstraint({"a": 0.0, "b": 0.0}, accessible_hours=0.0)
        self.assertEqual(automated.required_hours(output), 0.0)
        self.assertEqual(automated.feasible_scale(output), 1.0)
        self.assertIsNone(automated.binding(output))

    def test_b1_aggregate_hours_bind(self):
        output = dict(zip(TWO.sector_ids, TWO.gross_output(TWO_DEMAND)))
        required = TWO_OUTPUT[0] + 2.0 * TWO_OUTPUT[1]
        binding = AggregateLabourConstraint({"a": 1.0, "b": 2.0}, accessible_hours=100.0)
        self.assertTrue(close(binding.required_hours(output), required, 1e-12))
        self.assertEqual(binding.binding(output), "aggregate_hours")
        self.assertTrue(close(binding.feasible_scale(output), 100.0 / required, 1e-12))
        self.assertLess(binding.slack(output), 0.0)
        ample = AggregateLabourConstraint({"a": 1.0, "b": 2.0}, accessible_hours=10_000.0)
        self.assertEqual(ample.feasible_scale(output), 1.0)
        self.assertIsNone(ample.binding(output))

    def test_b2_skill_bottleneck_differs_from_b1(self):
        output = dict(zip(TWO.sector_ids, TWO.gross_output(TWO_DEMAND)))
        b1 = AggregateLabourConstraint({"a": 1.0, "b": 2.0}, accessible_hours=100.0)
        b2 = TaskLabourConstraint(
            {"a": {"manual": 1.0}, "b": {"manual": 2.0}},
            {"manual": 90.0},
            {"manual": "manual"},
        )
        by_skill = b2.required_by_skill(output)
        self.assertTrue(close(by_skill["manual"], TWO_OUTPUT[0] + 2.0 * TWO_OUTPUT[1], 1e-12))
        self.assertEqual(b2.binding(output), "manual")
        self.assertTrue(close(b2.feasible_scale(output), 90.0 / (TWO_OUTPUT[0] + 2.0 * TWO_OUTPUT[1]),
                              1e-12))
        self.assertLess(b2.feasible_scale(output), b1.feasible_scale(output))
        # a cognitive-skill bottleneck that the aggregate pool does not see
        cognitive = TaskLabourConstraint(
            {"a": {"manual": 0.5, "cognitive": 0.5}, "b": {"manual": 2.0}},
            {"manual": 1000.0, "cognitive": 5.0},
            {"cognitive": "cognitive"},
        )
        self.assertEqual(cognitive.binding(output), "cognitive")
        self.assertLess(cognitive.feasible_scale(output), 1.0)

    def test_labour_variant_comparison_has_no_ranking(self):
        output = dict(zip(TWO.sector_ids, TWO.gross_output(TWO_DEMAND)))
        variants = [NoLabourConstraint(), AggregateLabourConstraint({"a": 1.0, "b": 2.0}, 100.0),
                    TaskLabourConstraint({"a": {"manual": 1.0}, "b": {"manual": 2.0}},
                                         {"manual": 90.0})]
        rows = compare_labour_variants(output, variants)
        self.assertEqual([row["variant"] for row in rows], ["B0", "B1", "B2"])
        self.assertEqual(rows[0]["feasible_scale"], 1.0)
        self.assertIsInstance(rows[2]["required"], dict)  # B2 keeps the skill vector
        for row in rows:
            self.assertNotIn("best", row)  # R06: no ranking before an OOS test
            self.assertNotIn("winner", row)
        with self.assertRaises(LabourError):
            TaskLabourConstraint({"a": {"task": 1.0}}, {"skill": 10.0}, {"task": "missing_skill"})


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.output = dict(zip(TWO.sector_ids, TWO.gross_output(TWO_DEMAND)))
        self.energy = InputConstraint(dict(TWO_ENERGY_INTENSITY), available=50.0)
        self.labour = AggregateLabourConstraint({"a": 1.0, "b": 2.0}, accessible_hours=50.0)
        self.base = list(TWO_DEMAND)

    def test_energy_shock_reduces_output_and_keeps_balance(self):
        result = run_scenario(TWO, self.base, Scenario("energy_shock", energy_factor=1.0),
                              energy=self.energy)
        self.assertTrue(close(result.scale, 50.0 / TWO_ENERGY_REQUIRED, 1e-12))
        self.assertEqual(result.binding, ("energy",))
        self.assertTrue(close(result.energy["required_at_scaled_output"], 50.0, 1e-9))
        self.assertLess(result.value_added, 30.0)
        self.assertLessEqual(result.material_residual, 1e-9)
        for value in result.output.values():
            self.assertGreaterEqual(value, 0.0)

    def test_energy_availability_factor_enters_the_limit(self):
        full = run_scenario(TWO, self.base, Scenario("full", energy_factor=1.0), energy=self.energy)
        half = run_scenario(TWO, self.base, Scenario("half", energy_factor=0.5), energy=self.energy)
        self.assertTrue(close(half.scale, 0.5 * full.scale, 1e-12))
        self.assertTrue(close(half.energy["available"], 25.0, 1e-12))

    def test_labour_shock_and_combined_bottleneck(self):
        labour_only = run_scenario(TWO, self.base, Scenario("labour"), labour=self.labour)
        self.assertEqual(labour_only.binding, ("labour",))
        self.assertTrue(close(labour_only.scale, 50.0 / (TWO_OUTPUT[0] + 2.0 * TWO_OUTPUT[1]),
                              1e-12))
        combined = run_scenario(TWO, self.base, Scenario("combined", energy_factor=0.5,
                                                         labour_factor=0.75),
                                energy=self.energy, labour=self.labour)
        self.assertEqual(set(combined.binding), {"energy", "labour"})
        expected = min(0.5 * 50.0 / TWO_ENERGY_REQUIRED,
                       0.75 * 50.0 / (TWO_OUTPUT[0] + 2.0 * TWO_OUTPUT[1]))
        self.assertTrue(close(combined.scale, expected, 1e-12))
        self.assertLessEqual(combined.material_residual, 1e-9)
        self.assertTrue(combined.describe()["binding"])

    def test_zero_demand_and_demand_shock(self):
        zero = run_scenario(TWO, [0.0, 0.0], Scenario.baseline())
        self.assertEqual(zero.scale, 1.0)
        self.assertTrue(close(zero.value_added, 0.0))
        self.assertEqual(zero.output, {"a": 0.0, "b": 0.0})
        self.assertEqual(zero.binding, ())
        shock = Scenario("sector_a", demand_factors={"a": 0.0})
        result = run_scenario(TWO, self.base, shock)
        # a zero final demand for sector a still needs gross output of a for the b chain
        self.assertGreater(result.output["a"], 0.0)
        self.assertLess(result.output["a"], self.output["a"])
        self.assertTrue(close(result.value_added, sum(result.final_demand.values()), 1e-9))

    def test_scenario_validation(self):
        with self.assertRaises(ScenarioError):
            Scenario("bad", demand_factors={"a": -1.0})
        with self.assertRaises(ScenarioError):
            Scenario("bad", energy_factor=-0.5)
        with self.assertRaises(ScenarioError):
            run_scenario(TWO, self.base, Scenario("unknown_sector", demand_factors={"zzz": 0.5}))
        with self.assertRaises(ScenarioError):
            run_scenario(TWO, self.base, Scenario("missing_energy", energy_factor=0.5))
        with self.assertRaises(ScenarioError):
            run_scenario(TWO, self.base, Scenario("missing_labour", labour_factor=0.5))
        with self.assertRaises(ScenarioError):
            run_scenario(TWO, [-1.0, 2.0], Scenario.baseline())
        with self.assertRaises(ScenarioError):
            run_scenario(TWO, [1.0], Scenario.baseline())

    def test_standard_shock_set_and_comparison(self):
        rows = compare_scenarios(TWO, self.base, standard_shock_set(list(TWO.sector_ids)),
                                 energy=InputConstraint(dict(TWO_ENERGY_INTENSITY), 30.0),
                                 labour=AggregateLabourConstraint({"a": 1.0, "b": 2.0}, 60.0))
        self.assertEqual([row["scenario"] for row in rows],
                         ["baseline", "demand_shock", "energy_shock", "labour_shock",
                          "combined_shock"])
        baseline = rows[0]
        self.assertEqual(baseline["binding"], ["energy", "labour"])
        self.assertLessEqual(baseline["scale"], 1.0)
        for row in rows:
            self.assertLessEqual(row["material_residual"], 1e-9)
            for value in row["output"].values():
                self.assertGreaterEqual(value, 0.0)
        self.assertLess(rows[-1]["scale"], rows[1]["scale"])  # combined is tighter than demand alone
        self.assertLessEqual(rows[-1]["value_added"], rows[0]["value_added"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
