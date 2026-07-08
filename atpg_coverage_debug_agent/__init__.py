"""ATPG/DFT coverage-loss debugging agent.

This package provides a structural analysis engine, GUI, CLI and reporting
utilities to help hardware engineers understand *where* and *why* test
coverage is lost when running ATPG (Automatic Test Pattern Generation) on a
hierarchical gate-level Verilog netlist.

The tool is intentionally a *structural* analyzer. It does not simulate the
design nor implement a full Verilog language front-end. Every diagnosis it
emits is accompanied by evidence and a confidence level so that the engineer
can decide how much to trust each conclusion.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
