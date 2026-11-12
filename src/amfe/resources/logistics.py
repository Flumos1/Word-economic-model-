"""Supply-chain / logistics prototype: nodes, edges, delays, inventories (docs/38 block 08).

The prototype is a discrete-time material-flow model, not a route optimiser.  Every node is
a store/processing point with a physical inventory and a throughput limit; every edge is a
transport link with a capacity, a transport delay in whole periods and a loss fraction.
The accounting rules are the physical-stock identity (docs/38 I04) and the two
conservation checks below, so shipments are never double-counted at the source and at the
destination:

* per node and period: ``inventory_end = inventory_start + arrivals + exogenous_supply
  - shipped``;
* over the whole chain: ``initial_inventory + cumulative_supply = inventory + in_transit
  + cumulative_loss`` - mass that is neither in stock, in question, nor lost does not
  exist.

Demand that cannot be served is reported as ``unmet`` and is *not* a negative inventory:
the model refuses to hide a shortage inside an unphysical stock.  Disruptions are
scenario multipliers on edge capacity and node throughput, applied per period, so a
supply shock propagates downstream with the transport delay of the network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

TOLERANCE = 1e-9


class LogisticsError(ValueError):
    """Invalid network data or a violated conservation rule."""


def _non_negative(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise LogisticsError(f"{name} must be a finite non-negative number, got {value!r}")
    return number


@dataclass
class LogisticsNode:
    """A store or processing point with inventory and a per-period throughput limit."""

    node_id: str
    capacity: float = math.inf
    initial_inventory: float = 0.0
    unit: str = "unit"

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise LogisticsError("node_id must be a non-empty string")
        self.initial_inventory = _non_negative(self.initial_inventory, "initial_inventory")
        if self.capacity is not None:
            capacity = float(self.capacity)
            if math.isnan(capacity) or capacity < 0.0:
                raise LogisticsError("node capacity must be non-negative (or math.inf)")
            self.capacity = capacity
        self.inventory = self.initial_inventory


@dataclass
class LogisticsEdge:
    """A transport link: ``source -> target`` with capacity, delay and loss."""

    source: str
    target: str
    capacity: float
    delay: int = 0
    loss: float = 0.0
    unit: str = "unit"

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise LogisticsError("edge source must be a non-empty string")
        if not isinstance(self.target, str) or not self.target.strip():
            raise LogisticsError("edge target must be a non-empty string")
        if self.source == self.target:
            raise LogisticsError(f"self-loop edge {self.source!r} is not supported")
        self.capacity = _non_negative(self.capacity, "edge capacity")
        if not isinstance(self.delay, int) or isinstance(self.delay, bool) or self.delay < 1:
            raise LogisticsError(
                "edge delay must be an integer >= 1: in this discrete prototype every "
                "transport link takes at least one full period (delay=1 arrives next period)"
            )
        loss = float(self.loss)
        if not (0.0 <= loss < 1.0):
            raise LogisticsError("edge loss must be in [0, 1)")
        self.loss = loss

    @property
    def key(self) -> Tuple[str, str]:
        return (self.source, self.target)


@dataclass
class StepReport:
    """One period of the simulation (physical quantities, no valuation)."""

    period: int
    arrivals: Dict[str, float] = field(default_factory=dict)
    shipped: Dict[Tuple[str, str], float] = field(default_factory=dict)
    losses: Dict[Tuple[str, str], float] = field(default_factory=dict)
    inventory: Dict[str, float] = field(default_factory=dict)
    unmet: Dict[str, float] = field(default_factory=dict)
    in_transit: Dict[Tuple[str, str], float] = field(default_factory=dict)


class SupplyChain:
    """Discrete-time material-flow network with explicit delays, capacities and losses.

    ``supplies`` are exogenous inflows into a node in one period (domestic production,
    imports at a port, ...).  ``demand`` is the quantity a node is asked to ship out in the
    period; it is served from inventory and arrivals up to node throughput and then edge
    capacity.  Shipments arrive at the target after the edge's ``delay`` periods with the
    edge's loss fraction removed.
    """

    def __init__(self, nodes: Sequence[LogisticsNode], edges: Sequence[LogisticsEdge]) -> None:
        self.nodes: Dict[str, LogisticsNode] = {}
        for node in nodes:
            if node.node_id in self.nodes:
                raise LogisticsError(f"duplicate node {node.node_id!r}")
            self.nodes[node.node_id] = node
        if not self.nodes:
            raise LogisticsError("a supply chain needs at least one node")
        self.edges: List[LogisticsEdge] = list(edges)
        for edge in self.edges:
            if edge.source not in self.nodes:
                raise LogisticsError(f"edge source {edge.source!r} is not a node")
            if edge.target not in self.nodes:
                raise LogisticsError(f"edge target {edge.target!r} is not a node")
        self._out_edges: Dict[str, List[int]] = {node_id: [] for node_id in self.nodes}
        for index, edge in enumerate(self.edges):
            self._out_edges[edge.source].append(index)
        for node in self.nodes.values():
            if node.capacity < 0.0:
                raise LogisticsError("node capacities must be non-negative")
        self.reset()

    # -- state -------------------------------------------------------------------------
    def reset(self) -> None:
        """Return to the initial state (initial inventories, empty pipeline)."""
        for node in self.nodes.values():
            node.inventory = node.initial_inventory
        self.period = 0
        self._in_transit: List[Tuple[int, str, Tuple[str, str], float]] = []
        self._cumulative_supply: Dict[str, float] = {node_id: 0.0 for node_id in self.nodes}
        self._cumulative_arrivals: Dict[str, float] = {node_id: 0.0 for node_id in self.nodes}
        self._cumulative_shipped: Dict[str, float] = {node_id: 0.0 for node_id in self.nodes}
        self._cumulative_shipped_edges: Dict[Tuple[str, str], float] = {
            edge.key: 0.0 for edge in self.edges
        }
        self._cumulative_loss: Dict[Tuple[str, str], float] = {
            edge.key: 0.0 for edge in self.edges
        }
        self.history: List[StepReport] = []

    # -- simulation --------------------------------------------------------------------
    def _edge_factors(self, period: int,
                      disruptions: Optional[Mapping[int, Mapping[object, float]]]) -> Dict[int, float]:
        """Capacity multipliers of the edges for one period (default 1)."""
        factors = {index: 1.0 for index in range(len(self.edges))}
        if not disruptions:
            return factors
        layer = disruptions.get(period, {})
        for key, factor in layer.items():
            number = float(factor)
            if not math.isfinite(number) or number < 0.0:
                raise LogisticsError(
                    f"disruption factor must be finite and non-negative, got {factor!r}"
                )
            if isinstance(key, int):
                if not 0 <= key < len(self.edges):
                    raise LogisticsError(f"edge index {key} out of range")
                factors[key] *= number
            elif isinstance(key, str):
                if key not in self.nodes:
                    raise LogisticsError(f"disruption refers to unknown node {key!r}")
            elif isinstance(key, tuple) and len(key) == 2:
                matches = [i for i, e in enumerate(self.edges) if e.key == key]
                if not matches:
                    raise LogisticsError(f"disruption refers to unknown edge {key!r}")
                for index in matches:
                    factors[index] *= number
            else:
                raise LogisticsError(f"unsupported disruption key {key!r}")
        return factors

    def step(self, supplies: Optional[Mapping[str, float]] = None,
             demand: Optional[Mapping[str, float]] = None,
             disruptions: Optional[Mapping[int, Mapping[object, float]]] = None) -> StepReport:
        """Advance one period and return the physical flow report."""
        period = self.period + 1
        self.period = period
        supplies = dict(supplies or {})
        demand = dict(demand or {})
        for node_id in supplies:
            if node_id not in self.nodes:
                raise LogisticsError(f"supply for unknown node {node_id!r}")
        for node_id in demand:
            if node_id not in self.nodes:
                raise LogisticsError(f"demand for unknown node {node_id!r}")
        inflow = {node_id: _non_negative(supplies.get(node_id, 0.0), f"supply[{node_id}]")
                  for node_id in self.nodes}
        requested = {node_id: _non_negative(demand.get(node_id, 0.0), f"demand[{node_id}]")
                     for node_id in self.nodes}
        node_factors = {node_id: 1.0 for node_id in self.nodes}
        for key, factor in (disruptions or {}).get(period, {}).items():
            if isinstance(key, str) and key in self.nodes:
                node_factors[key] *= float(factor)

        # 1. arrivals due this period (delayed shipments already lost their loss fraction)
        report = StepReport(period=period)
        arrivals = {node_id: 0.0 for node_id in self.nodes}
        remaining: List[Tuple[int, str, Tuple[str, str], float]] = []
        for arrival_period, target, key, amount in self._in_transit:
            if arrival_period <= period:
                arrivals[target] += amount
                self._cumulative_arrivals[target] += amount
            else:
                remaining.append((arrival_period, target, key, amount))
        self._in_transit = remaining

        # 2. available stock: start-of-period inventory + arrivals + exogenous supply
        available: Dict[str, float] = {}
        for node_id, node in self.nodes.items():
            available[node_id] = node.inventory + arrivals[node_id] + inflow[node_id]
            self._cumulative_supply[node_id] += inflow[node_id]

        # 3. dispatch: walk the node's outgoing edges in declaration order
        edge_factors = self._edge_factors(period, disruptions)
        shipped: Dict[Tuple[str, str], float] = {edge.key: 0.0 for edge in self.edges}
        losses: Dict[Tuple[str, str], float] = {edge.key: 0.0 for edge in self.edges}
        for node_id, node in self.nodes.items():
            remaining_demand = requested[node_id]
            if remaining_demand <= TOLERANCE:
                continue
            throughput = node.capacity * node_factors[node_id]
            for index in self._out_edges[node_id]:
                if remaining_demand <= TOLERANCE or throughput <= TOLERANCE:
                    break
                edge = self.edges[index]
                capacity = edge.capacity * edge_factors[index]
                amount = min(remaining_demand, capacity, available[node_id], throughput)
                if amount <= 0.0:
                    continue
                available[node_id] -= amount
                throughput -= amount
                remaining_demand -= amount
                shipped[edge.key] += amount
                self._cumulative_shipped[node_id] += amount
                self._cumulative_shipped_edges[edge.key] += amount
                lost = amount * edge.loss
                delivered = amount - lost
                losses[edge.key] += lost
                self._cumulative_loss[edge.key] += lost
                self._in_transit.append((period + edge.delay, edge.target, edge.key, delivered))
            report.unmet[node_id] = max(0.0, remaining_demand)

        # 4. end-of-period inventory per node: physical stock identity (docs/38 I04)
        for node_id, node in self.nodes.items():
            node.inventory = available[node_id]
            if node.inventory < -TOLERANCE:
                raise LogisticsError(f"negative inventory at node {node_id!r}")
            report.inventory[node_id] = node.inventory
        report.arrivals = arrivals
        report.shipped = shipped
        report.losses = losses
        report.in_transit = {key: 0.0 for key in self._cumulative_shipped_edges}
        for _, _, key, amount in self._in_transit:
            report.in_transit[key] += amount
        self.history.append(report)
        return report

    def run(self, horizon: int,
            supplies: Optional[Mapping[int, Mapping[str, float]]] = None,
            demand: Optional[Mapping[int, Mapping[str, float]]] = None,
            disruptions: Optional[Mapping[int, Mapping[object, float]]] = None) -> List[StepReport]:
        """Run ``horizon`` periods; supplies/demand/disruptions are keyed by period (1-based)."""
        if not isinstance(horizon, int) or horizon < 0:
            raise LogisticsError("horizon must be a non-negative integer")
        supplies = supplies or {}
        demand = demand or {}
        reports = []
        for period in range(1, horizon + 1):
            reports.append(self.step(supplies.get(period, {}), demand.get(period, {}), disruptions))
        return reports

    # -- invariants --------------------------------------------------------------------
    def check_node_conservation(self, tolerance: float = TOLERANCE) -> Dict[str, float]:
        """Per-node stock identity over the whole run; raises on violation."""
        residuals = {}
        for node_id, node in self.nodes.items():
            residual = (
                node.initial_inventory
                + self._cumulative_supply[node_id]
                + self._cumulative_arrivals[node_id]
                - self._cumulative_shipped[node_id]
                - node.inventory
            )
            residuals[node_id] = residual
            if abs(residual) > tolerance:
                raise LogisticsError(
                    f"node {node_id!r} violates the stock identity by {residual:.3e}"
                )
        return residuals

    def check_mass_conservation(self, tolerance: float = TOLERANCE) -> float:
        """Chain-wide mass balance: initial + supplied = inventory + in transit + lost."""
        initial = math.fsum(node.initial_inventory for node in self.nodes.values())
        supplied = math.fsum(self._cumulative_supply.values())
        inventory = math.fsum(node.inventory for node in self.nodes.values())
        in_transit = math.fsum(amount for _, _, _, amount in self._in_transit)
        lost = math.fsum(self._cumulative_loss.values())
        residual = initial + supplied - inventory - in_transit - lost
        if abs(residual) > tolerance:
            raise LogisticsError(f"chain mass balance violated by {residual:.3e}")
        return residual

    def in_transit_total(self) -> float:
        return math.fsum(amount for _, _, _, amount in self._in_transit)

    def unmet_total(self) -> float:
        return math.fsum(math.fsum(report.unmet.values()) for report in self.history)

    def describe(self) -> dict:
        return {
            "nodes": sorted(self.nodes),
            "edges": [f"{edge.source}->{edge.target}" for edge in self.edges],
            "horizon": len(self.history),
            "unmet_total": round(self.unmet_total(), 12),
            "in_transit_total": round(self.in_transit_total(), 12),
        }


def chain_topology(nodes: Sequence[str], links: Sequence[Tuple[str, str, float, int, float]]
                   ) -> SupplyChain:
    """Convenience constructor from ``(source, target, capacity, delay, loss)`` tuples."""
    node_objects = [LogisticsNode(node_id) for node_id in nodes]
    edge_objects = [
        LogisticsEdge(source, target, capacity, delay, loss)
        for source, target, capacity, delay, loss in links
    ]
    return SupplyChain(node_objects, edge_objects)


def supply_shock(chain: SupplyChain, horizon: int,
                 shock_periods: Iterable[int],
                 edge_factor: float = 0.0,
                 supplies: Optional[Mapping[int, Mapping[str, float]]] = None,
                 demand: Optional[Mapping[int, Mapping[str, float]]] = None) -> dict:
    """Apply ``edge_factor`` to every edge during ``shock_periods`` and report the damage.

    Returns the per-period unmet demand of the shocked run, so a downstream shortage and
    its recovery can be compared with the undisrupted baseline.
    """
    chain.reset()
    disruptions = {period: {index: edge_factor for index in range(len(chain.edges))}
                   for period in shock_periods}
    reports = chain.run(horizon, supplies, demand, disruptions)
    return {
        "periods": horizon,
        "shock_periods": sorted(shock_periods),
        "edge_factor": edge_factor,
        "unmet_by_period": [round(math.fsum(report.unmet.values()), 12) for report in reports],
        "unmet_total": round(chain.unmet_total(), 12),
        "conservation_error": chain.check_node_conservation(),
        "mass_error": round(chain.check_mass_conservation(), 15),
    }
