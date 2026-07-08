"""Tests for the root-cause engine on small synthetic examples."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.analysis.root_cause import RootCauseEngine
from atpg_coverage_debug_agent.models import FaultClass, RootCause
from atpg_coverage_debug_agent.parser.constraint_parser import parse_constraints
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog_file


def _engine(netlist_path, constraint_text=""):
    netlist = parse_verilog_file(netlist_path)
    conn = ConnectivityModel(netlist)
    mapper = FaultMapper(conn)
    constraints, _ = parse_constraints(constraint_text)
    return RootCauseEngine(conn, mapper, constraints)


def test_tied_constant_detected(sample_netlist_path):
    engine = _engine(sample_netlist_path)
    faults, _ = parse_fault_list("AU 0 top/u_alu/U_tie/Y")
    result = engine.analyze_fault(faults[0])
    assert result.root_cause is RootCause.TIED_OR_CONSTANT


def test_unresolved_connectivity(sample_netlist_path):
    engine = _engine(sample_netlist_path)
    faults, _ = parse_fault_list("AU 0 top/ghost/Znn/Y")
    result = engine.analyze_fault(faults[0])
    assert result.root_cause is RootCause.UNRESOLVED_CONNECTIVITY
    assert result.recommended_step


def test_constraint_induced_loss(sample_netlist_path):
    engine = _engine(sample_netlist_path, "force sel 0")
    faults, _ = parse_fault_list("UC 0 top/u_alu/U3/S")
    result = engine.analyze_fault(faults[0])
    # The select net 'sel' is constrained; expect a constraint-related cause.
    assert result.constraint_related
    assert result.root_cause in (
        RootCause.CONSTRAINT_CONTROLLABILITY,
        RootCause.CONSTRAINT_OBSERVABILITY,
        RootCause.CLOCK_RESET_TE_BLOCKING,
    )


def test_evidence_separates_observed_and_inferred(sample_netlist_path):
    engine = _engine(sample_netlist_path)
    faults, _ = parse_fault_list("UO 0 top/u_ctrl/reg_nonscan/Q")
    result = engine.analyze_fault(faults[0])
    assert result.observed_facts
    assert result.inferred_conclusions
    assert result.fault.fault_class is FaultClass.UO
