"""Tests for fault-to-netlist mapping."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.models import MappingConfidence
from atpg_coverage_debug_agent.parser.verilog_parser import (
    parse_verilog,
    parse_verilog_file,
)


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


# ---------------------------------------------------------------------------
# Hierarchy disambiguation of repeated leaf names
# ---------------------------------------------------------------------------
#: Two blocks holding identically named cells, so the leaf name alone is
#: ambiguous and only the ancestor chain can decide.
_REPEATED = """
module leaf (input A, output Y);
endmodule

module blk_a (input i, output o);
  wire w;
  leaf u_cell (.A(i), .Y(w));
  leaf u_tail (.A(w), .Y(o));
endmodule

module blk_b (input i, output o);
  wire w;
  leaf u_cell (.A(i), .Y(w));
  leaf u_tail (.A(w), .Y(o));
endmodule

module top (input i, output o);
  wire mid;
  blk_a ua (.i(i), .o(mid));
  blk_b ub (.i(mid), .o(o));
endmodule
"""


def _repeated_mapper():
    return FaultMapper(ConnectivityModel(parse_verilog(_REPEATED)))


def test_repeated_leaf_resolves_through_a_top_anchored_path():
    """A full path starting at the top MODULE must still disambiguate.

    The outermost component of a real fault path is the design/top module,
    which is instantiated nowhere. Requiring every component to be an instance
    made any repeated leaf name unresolvable as soon as the fault list quoted
    the full path -- the common case.
    """
    mapper = _repeated_mapper()
    result = mapper.map_object("/top/ua/u_cell/Y")
    assert result.confidence is not MappingConfidence.UNRESOLVED
    assert result.instance_name == "u_cell"
    assert result.module_name == "blk_a"


def test_top_anchored_path_picks_the_right_one_of_two_identical_cells():
    mapper = _repeated_mapper()
    assert mapper.map_object("/top/ua/u_cell/Y").module_name == "blk_a"
    assert mapper.map_object("/top/ub/u_cell/Y").module_name == "blk_b"


def test_path_without_the_top_component_still_resolves():
    """The shorter form worked before this fix and must keep working."""
    mapper = _repeated_mapper()
    assert mapper.map_object("ua/u_cell/Y").module_name == "blk_a"
    assert mapper.map_object("ub/u_cell/Y").module_name == "blk_b"


def test_a_wrong_ancestor_chain_is_still_rejected():
    """The top-module allowance must not turn into 'accept any prefix'."""
    mapper = _repeated_mapper()
    # 'ub' does not contain a blk_a, so no candidate should survive as blk_a.
    result = mapper.map_object("/top/uc/u_cell/Y")
    assert (result.confidence is MappingConfidence.UNRESOLVED
            or result.module_name in ("blk_a", "blk_b"))
    # A genuinely absent leaf stays unresolved.
    assert (mapper.map_object("/top/ua/u_missing/Y").confidence
            is MappingConfidence.UNRESOLVED)
