"""Tests for tracing what structurally blocks coverage-loss faults."""

from __future__ import annotations

import pytest

from atpg_coverage_debug_agent.analysis.attribution import (
    Attributor,
    attribute_categories,
    attribute_pin_constraints,
    attribute_tie_sources,
)
from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.analysis.root_cause import RootCauseEngine
from atpg_coverage_debug_agent.parser.constraint_parser import parse_constraints
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog

NETLIST = """
module blk (clk, pi_hold, a, y, z, w);
  input  clk, pi_hold, a;
  output y, z, w;
  wire   c_tie, c_tdr, c_nsff, n1, n2, n3;

  // A hardwired constant driver.
  TIEHI  u_tiehi ( .Y(c_tie) );
  AND2   u_after_tie ( .A(c_tie), .B(a), .Y(n1) );
  BUF    u_sink_tie ( .A(n1), .Y(y) );

  // A test data register output: configurable per run.
  DFF    tdr_out_inter_reg_0 ( .D(pi_hold), .CK(clk), .Q(c_tdr) );
  AND2   u_after_tdr ( .A(c_tdr), .B(a), .Y(n2) );
  BUF    u_sink_tdr ( .A(n2), .Y(z) );

  // An unscanned sequential element: ATPG cannot control it.
  DFF_nsff u_nonscan ( .D(a), .CK(clk), .Q(c_nsff) );
  AND2   u_after_nsff ( .A(c_nsff), .B(a), .Y(n3) );
  BUF    u_sink_nsff ( .A(n3), .Y(w) );
endmodule
"""

CONSTRAINTS = """
add_input_constraints pi_hold C1
add_input_constraints a CX
"""


def _analysed(fault_text, constraint_text=CONSTRAINTS):
    """Parse inputs and run the mapping/root-cause pass the pipeline uses."""
    netlist = parse_verilog(NETLIST)
    faults, _ = parse_fault_list(fault_text)
    constraints, _ = parse_constraints(constraint_text)

    conn = ConnectivityModel(netlist)
    engine = RootCauseEngine(conn, FaultMapper(conn), constraints)
    results = [engine.analyze_fault(f) for f in faults if f.is_coverage_loss]
    return conn, constraints, results


# ---------------------------------------------------------------------------
# Tie sources
# ---------------------------------------------------------------------------
def test_hardwired_tie_is_named_and_reported_as_unfixable():
    conn, constraints, results = _analysed(
        "AU.TC 1 blk/u_sink_tie/Y\nAU.TC 0 blk/u_after_tie/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))

    assert att.attributed == 2
    top = att.top_tie
    assert top.driver == "u_tiehi"
    assert top.tie_value == "1"
    assert top.kind == "tie_cell"
    assert not top.is_configurable
    assert att.verdict == "hardwired_tie"
    # A hardwired tie cannot be undone from the ATPG side.
    assert att.preferred_fix_ids == ["tc_rtl_change"]


def test_test_data_register_is_recognised_as_configurable():
    conn, constraints, results = _analysed("AU.TC 1 blk/u_sink_tdr/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))

    top = att.top_tie
    assert top.driver == "tdr_out_inter_reg_0"
    assert top.kind == "test_data_register"
    assert top.is_configurable
    assert att.verdict == "configurable_register"
    assert att.preferred_fix_ids == ["tc_tdr_topoff"]
    assert "topoff" in att.note


def test_unscanned_flop_is_distinguished_from_a_tie_cell():
    conn, constraints, results = _analysed("AU.TC 0 blk/u_sink_nsff/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))

    assert att.top_tie.kind == "non_scan_flop"
    assert att.verdict == "non_scan_drive"
    assert "DFT change" in att.note


def test_sources_are_ranked_by_fault_count():
    conn, constraints, results = _analysed(
        "AU.TC 1 blk/u_sink_tdr/Y\n"
        "AU.TC 0 blk/u_after_tdr/Y\n"
        "AU.TC 1 blk/u_sink_tie/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))

    assert [s.driver for s in att.tie_sources][0] == "tdr_out_inter_reg_0"
    assert att.tie_sources[0].count == 2


def test_samples_are_verbatim_fault_paths():
    conn, constraints, results = _analysed("AU.TC 1 blk/u_sink_tdr/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))
    assert att.top_tie.samples == ["blk/u_sink_tdr/Y"]


def test_unmappable_faults_are_counted_not_silently_dropped():
    conn, constraints, results = _analysed("AU.TC 1 nowhere/does_not_exist/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))

    assert att.analysed == 1
    assert att.attributed == 0
    assert att.unresolved_mapping == 1
    assert att.verdict == "inconclusive"
    assert "could not be mapped" in att.note


def test_partial_attribution_is_declared_as_partial():
    conn, constraints, results = _analysed(
        "AU.TC 1 blk/u_sink_tdr/Y\n"
        "AU.TC 1 nowhere/a/Y\nAU.TC 1 nowhere/b/Y\nAU.TC 1 nowhere/c/Y\n")
    att = attribute_tie_sources(results, Attributor(conn, constraints))

    assert att.coverage < 0.5
    assert "partial picture" in att.note


# ---------------------------------------------------------------------------
# Pin constraints
# ---------------------------------------------------------------------------
def test_pin_constraints_are_traced_to_the_constrained_signal():
    conn, constraints, results = _analysed("AU.PC 1 blk/u_after_tdr/Y\n")
    att = attribute_pin_constraints(results, Attributor(conn, constraints))

    assert att.attributed == 1
    signals = {s.signal for s in att.constraint_sources}
    assert "pi_hold" in signals or "a" in signals


def test_a_few_named_fixed_pins_read_as_configured_loss():
    conn, constraints, results = _analysed(
        "AU.PC 1 blk/u_after_tdr/Y\n",
        constraint_text="add_input_constraints pi_hold C1\n")
    att = attribute_pin_constraints(results, Attributor(conn, constraints))

    assert att.verdict == "user_configured"
    assert "pc_waive_named" in att.preferred_fix_ids
    assert "configured loss" in att.note


def test_masked_constraints_read_as_a_cross_partition_suspicion():
    conn, constraints, results = _analysed(
        "AU.PC 1 blk/u_after_tdr/Y\n",
        constraint_text="add_input_constraints a CX\n")
    att = attribute_pin_constraints(results, Attributor(conn, constraints))

    assert att.verdict == "diffuse_or_masked"
    assert att.preferred_fix_ids == ["pc_unwrapped_whatif"]
    assert "another partition" in att.note


def test_no_constraints_yields_an_honest_inconclusive():
    conn, constraints, results = _analysed(
        "AU.PC 1 blk/u_after_tdr/Y\n", constraint_text="")
    att = attribute_pin_constraints(results, Attributor(conn, constraints))

    assert att.attributed == 0
    assert att.verdict == "inconclusive"


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------
def test_attribution_promotes_the_fix_the_evidence_points_at():
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )
    from atpg_coverage_debug_agent.analysis.statistics import (
        compute_statistics,
        enrich_categories,
        select_categories,
    )

    text = "\n".join(f"AU.TC 1 blk/u_sink_tdr/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    faults = [r.fault for r in results]

    stats = compute_statistics(faults)
    selected = enrich_categories(select_categories(stats), faults)
    attribute_categories(selected, results, conn, constraints)

    category = selected[0]
    assert category.attribution.verdict == "configurable_register"

    recs = build_recommendations(stats, selected)
    # Cost-based ordering would not put the topoff first on its own; the
    # traced register is what promotes it.
    assert recs[0].fix.fix_id == "tc_tdr_topoff"
    assert any("tdr_out_inter_reg_0" in line for line in recs[0].evidence)


def test_categories_without_a_tracer_are_left_alone():
    conn, constraints, results = _analysed(
        "\n".join(f"UO.AAB 1 blk/u_sink_tdr/Y{i}" for i in range(12)))
    from atpg_coverage_debug_agent.analysis.statistics import (
        compute_statistics,
        select_categories,
    )

    faults = [r.fault for r in results]
    selected = select_categories(compute_statistics(faults))
    attribute_categories(selected, results, conn, constraints)
    assert selected[0].attribution is None


def test_blocking_sources_tool_reads_the_serialized_payload():
    from atpg_coverage_debug_agent.analysis import investigate
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )
    from atpg_coverage_debug_agent.analysis.statistics import (
        compute_statistics,
        enrich_categories,
        select_categories,
    )

    text = "\n".join(f"AU.TC 1 blk/u_sink_tdr/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    faults = [r.fault for r in results]

    stats = compute_statistics(faults)
    selected = enrich_categories(select_categories(stats), faults)
    attribute_categories(selected, results, conn, constraints)
    payload = investigate.serialize_triage(
        stats, selected, build_recommendations(stats, selected))

    data = investigate.run_tool(
        "list_blocking_sources", {}, fault_results=[], constraints=[],
        netlist=None, triage=payload)
    assert data["categories"]
    entry = data["categories"][0]
    assert entry["verdict"] == "configurable_register"
    assert entry["tie_sources"][0]["driver"] == "tdr_out_inter_reg_0"
    assert "estimate" in data["note"]


def test_attribution_is_absent_without_a_netlist():
    from atpg_coverage_debug_agent.analysis.statistics import (
        compute_statistics,
        select_categories,
    )

    conn, constraints, results = _analysed("AU.TC 1 blk/u_sink_tdr/Y\n")
    faults = [r.fault for r in results]
    selected = select_categories(compute_statistics(faults))
    attribute_categories(selected, results, None, constraints)
    assert all(getattr(c, "attribution", None) is None for c in selected)
