"""Tests for fault-to-netlist mapping."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.models import MappingConfidence
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog_file


def _mapper(path):
    netlist = parse_verilog_file(path)
    return FaultMapper(ConnectivityModel(netlist))


def test_unique_instance_medium_confidence(sample_netlist_path):
    mapper = _mapper(sample_netlist_path)
    result = mapper.map_object("top/u_alu/U1/Y")
    assert result.instance_name == "U1"
    assert result.confidence in (MappingConfidence.MEDIUM,
                                 MappingConfidence.HIGH)


def test_unresolved_for_unknown_object(sample_netlist_path):
    mapper = _mapper(sample_netlist_path)
    result = mapper.map_object("top/does_not_exist/Z9/Y")
    assert result.confidence is MappingConfidence.UNRESOLVED
    assert result.instance_name is None


def test_evidence_is_attached(sample_netlist_path):
    mapper = _mapper(sample_netlist_path)
    result = mapper.map_object("top/u_ctrl/U5/Y")
    assert result.evidence  # never silent
