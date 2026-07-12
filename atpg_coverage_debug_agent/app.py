"""High-level application service tying parsing and analysis together.

Both the CLI and GUI call into :func:`run_analysis` / :func:`analyze_paths`
so the orchestration logic lives in exactly one place.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from .analysis.summarizer import build_report
from .models import AnalysisReport
from .parser.constraint_parser import parse_constraints_file
from .parser.fault_parser import parse_fault_list_file
from .parser.verilog_parser import parse_verilog_file

logger = logging.getLogger(__name__)


@dataclass
class AnalysisInputs:
    """Resolved input paths for one analysis run."""

    netlist_path: str
    faults_path: str
    constraints_path: Optional[str] = None


def _validate(path: str, label: str) -> None:
    if not path:
        raise ValueError(f"{label} path was not provided.")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{label} file not found: {path}")


def analyze_paths(inputs: AnalysisInputs, progress=None,
                  skill_manager=None) -> AnalysisReport:
    """Parse the three artefacts and run the analysis pipeline.

    Args:
        inputs: Resolved input paths.
        progress: Optional ``callable(done, total, msg)`` progress callback.
        skill_manager: Optional SkillManager to run analysis skills.

    Returns:
        A populated :class:`AnalysisReport`.

    Raises:
        FileNotFoundError / ValueError: when required inputs are missing.
    """
    _validate(inputs.netlist_path, "Netlist")
    _validate(inputs.faults_path, "Fault list")

    warnings: List[str] = []

    if progress:
        progress(0, 5, "Parsing netlist")
    netlist = parse_verilog_file(inputs.netlist_path)

    if progress:
        progress(1, 5, "Parsing fault list")
    faults, fault_warnings = parse_fault_list_file(inputs.faults_path)
    warnings.extend(fault_warnings)

    constraints = []
    if inputs.constraints_path:
        if os.path.isfile(inputs.constraints_path):
            if progress:
                progress(2, 5, "Parsing constraints")
            constraints, con_warnings = parse_constraints_file(
                inputs.constraints_path)
            warnings.extend(con_warnings)
        else:
            warnings.append(
                f"Constraint file not found: {inputs.constraints_path}"
            )

    if progress:
        progress(3, 5, "Running analysis")
    report = build_report(netlist, faults, constraints, warnings,
                          progress=progress)

    # Retain the parsed artefacts so the agentic AI layer can build a live
    # AnalysisContext and invoke skills as tools on demand.
    report.netlist = netlist
    report.faults = faults
    report.constraints = constraints

    if skill_manager is not None:
        if progress:
            progress(4, 5, "Running skills")
        from .skills.base import AnalysisContext
        ctx = AnalysisContext(
            netlist=netlist,
            faults=faults,
            constraints=constraints,
            fault_results=report.fault_results,
            pattern_groups=report.pattern_groups,
            summary=report.summary,
        )
        report.skill_results = skill_manager.run_all(ctx)

    if progress:
        progress(5, 5, "Complete")
    return report


def run_analysis(netlist_path: str, faults_path: str,
                 constraints_path: Optional[str] = None,
                 progress=None, skill_manager=None) -> AnalysisReport:
    """Convenience wrapper accepting raw path strings."""
    return analyze_paths(
        AnalysisInputs(netlist_path, faults_path, constraints_path),
        progress=progress,
        skill_manager=skill_manager,
    )
