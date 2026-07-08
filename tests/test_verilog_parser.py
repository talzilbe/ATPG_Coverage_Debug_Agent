"""Tests for the structural Verilog parser and connectivity model."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.parser.verilog_parser import (
    parse_verilog,
    parse_verilog_file,
)

SIMPLE = """
module top (a, b, y);
  input a, b;
  output y;
  wire n1;
  AND2 U1 ( .A(a), .B(b), .Y(n1) );
  INV  U2 ( .A(n1), .Y(y) );
endmodule
"""


def test_parses_modules_and_instances():
    netlist = parse_verilog(SIMPLE)
    assert "top" in netlist.modules
    top = netlist.modules["top"]
    assert set(top.instances) == {"U1", "U2"}
    assert top.instances["U1"].cell_type == "AND2"


def test_port_directions():
    netlist = parse_verilog(SIMPLE)
    top = netlist.modules["top"]
    dirs = {p.name: p.direction for p in top.ports}
    assert dirs["a"] == "input"
    assert dirs["y"] == "output"


def test_connectivity_fan_in_out():
    netlist = parse_verilog(SIMPLE)
    conn = ConnectivityModel(netlist)
    # U2 input n1 is driven by U1 -> U1 is fan-in of U2.
    assert "U1" in conn.immediate_fan_in("top", "U2")
    assert "U2" in conn.immediate_fan_out("top", "U1")


def test_sample_file_parses(sample_netlist_path):
    netlist = parse_verilog_file(sample_netlist_path)
    assert "top" in netlist.modules
    assert "alu_block" in netlist.modules
    assert "ctrl_block" in netlist.modules
    # The top is never instantiated, so it should be inferred as top.
    assert netlist.top_module == "top"


def test_cone_trace_bounded(sample_netlist_path):
    netlist = parse_verilog_file(sample_netlist_path)
    conn = ConnectivityModel(netlist)
    cone = conn.trace_cone("alu_block", "reg_scan", direction="in",
                           max_depth=3)
    names = {n for n, _d in cone}
    # MUX drives the scan flop's D input.
    assert "U3" in names
