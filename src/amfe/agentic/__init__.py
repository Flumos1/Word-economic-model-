"""Agentic economy simulator (minimal ABM with SFC consistency).

Public API:

* :class:`~amfe.agentic.simulator.Parameters` -- frozen parameter set (agents, seed, shock, AI share);
* :class:`~amfe.agentic.simulator.Ledger` -- double-entry ledger; every transaction must sum to zero
  over accounts (fail-closed :class:`LedgerError`);
* :class:`~amfe.agentic.simulator.AgenticSimulator` -- deterministic period simulation of households,
  firms, a bank and the government;
* :class:`~amfe.agentic.simulator.SimulationResult` / ``PeriodResult`` -- aggregates and checks;
* :func:`~amfe.agentic.simulator.run_demo` -- the documented demo (income shock + AI procurement share
  0 -> 50%);
* :func:`~amfe.agentic.simulator.format_demo` -- plain-text report of a demo run.

The simulator is a **research toy**: it demonstrates accounting consistency (double entry, closed SFC)
and mechanism design (income shock, AI procurement share), not real-world behaviour. It is not a
forecast, not an admission and not investment advice.
"""
from __future__ import annotations

from .simulator import (
    AgenticSimulator,
    ConvergenceError,
    Ledger,
    LedgerError,
    ParameterError,
    Parameters,
    PeriodResult,
    SFCError,
    SimulationError,
    SimulationResult,
    format_demo,
    run_demo,
)

__all__ = [
    "AgenticSimulator",
    "ConvergenceError",
    "Ledger",
    "LedgerError",
    "ParameterError",
    "Parameters",
    "PeriodResult",
    "SFCError",
    "SimulationError",
    "SimulationResult",
    "format_demo",
    "run_demo",
]
