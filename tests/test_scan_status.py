"""Acceptance tests for the scan-status verdict and the tied-constant fix.

These encode the two conditions the fix must satisfy together:

* **Test 1** -- with the netlist, the agent answers SCAN for
  ``.../boot_seq_ctrl/state_out_reg`` and quotes the
  ``.si`` / ``.ssb`` / ``.so`` pins from the instantiation.
* **Test 2** -- without the netlist, given only a fault-table row
  (``mapped_instance='-'``, ``confidence='unresolved'``, ``fanin=0``,
  ``fanout=0``, ``scan='N'``), the agent answers exactly
  "Unresolved - scan status cannot be determined without netlist pin
  evidence." and does not guess "non-scan".

Passing Test 1 alone would only prove the answer for this one cell has been
memorised; Test 2 is what proves the reasoning defect is gone.
"""

from __future__ import annotations

import pytest

from atpg_coverage_debug_agent.agent.debug_agent import (
    SYSTEM_PROMPT, build_user_payload)
from atpg_coverage_debug_agent.analysis import investigate, scan_status
from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.analysis.root_cause import RootCauseEngine
from atpg_coverage_debug_agent.analysis.summarizer import build_report
from atpg_coverage_debug_agent.models import (
    FaultClass, FaultRecord, MappingConfidence, RootCause)
from atpg_coverage_debug_agent.parser.fault_parser import normalize_object
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog
from atpg_coverage_debug_agent.reporting import csv_report, markdown_report


# The instance under test, exactly as it was asked about.
TARGET = ("/core/core_inst/clkblk/bootfsm/"
          "boot_seq_ctrl/state_out_reg")


# ---------------------------------------------------------------------------
# A miniature of the hierarchy that produced the original wrong answer.
#
# Reproduces the three properties that broke that analysis:
#   1. the leaf name state_out_reg is defined in several modules, so a
#      leaf-name lookup is ambiguous;
#   2. the scan flop's .d pin is a feedthrough port four levels deep;
#   3. the net at the far end is driven by a tie-high cell with no inputs.
#
# The cell names are deliberately free of any scan-ish substring (no "sff",
# "sdff", "scan"). The SCAN verdict therefore cannot come from naming -- only
# the .si / .ssb / .so pin evidence can produce it, which is the whole point.
# ---------------------------------------------------------------------------
NETLIST = """
module demo_flop_xover ( d_in, set_cond, reset_bar, gated_clk,
  scan_in_net, shift_en_net, q_out, scan_out_net );
  input d_in, set_cond, reset_bar, gated_clk;
  input scan_in_net, shift_en_net;
  output q_out, scan_out_net;
  vendorcell_a1 state_out_reg ( .si ( scan_in_net ) ,
  .rb ( reset_bar ) , .d ( d_in ) , .den ( set_cond ) ,
  .ssb ( shift_en_net ) , .clk ( gated_clk ) , .o ( q_out ) ,
  .so ( scan_out_net ) ) ;
endmodule

module demo_decoy_a ( d, clk, o );
  input d, clk;
  output o;
  vendorcell_b2 state_out_reg ( .d ( d ) , .clk ( clk ) , .o ( o ) ) ;
endmodule

module demo_decoy_b ( d, clk, o );
  input d, clk;
  output o;
  vendorcell_b2 state_out_reg ( .d ( d ) , .clk ( clk ) , .o ( o ) ) ;
endmodule

module demo_bootfsm ( p20001, se20001, si20001, cond20001,
  rst20001, clk20001, q20001, so20001 );
  input p20001, se20001, si20001, cond20001, rst20001, clk20001;
  output q20001, so20001;
  demo_flop_xover boot_seq_ctrl (
    .d_in ( p20001 ) , .set_cond ( cond20001 ) ,
    .reset_bar ( rst20001 ) , .gated_clk ( clk20001 ) ,
    .scan_in_net ( si20001 ) , .shift_en_net ( se20001 ) ,
    .q_out ( q20001 ) , .scan_out_net ( so20001 ) ) ;
endmodule

module demo_clkblk ( p30001, se30001, si30001, cond30001,
  rst30001, clk30001, q30001, so30001 );
  input p30001, se30001, si30001, cond30001, rst30001, clk30001;
  output q30001, so30001;
  demo_bootfsm bootfsm ( .p20001 ( p30001 ) ,
    .se20001 ( se30001 ) , .si20001 ( si30001 ) , .cond20001 ( cond30001 ) ,
    .rst20001 ( rst30001 ) , .clk20001 ( clk30001 ) , .q20001 ( q30001 ) ,
    .so20001 ( so30001 ) ) ;
endmodule

module demo_core ( test_se, chain_head, cond_in, rst_in, clk_in,
  q_out, chain_tail );
  input test_se, chain_head, cond_in, rst_in, clk_in;
  output q_out, chain_tail;
  wire tied_net, scan_en_net;
  TIEHIX1 u_tie_hi ( .o ( tied_net ) ) ;
  BUFX2 se_buf ( .a ( test_se ) , .o ( scan_en_net ) ) ;
  BUFX2 si_buf ( .a ( chain_head ) , .o ( chain_net ) ) ;
  demo_clkblk clkblk ( .p30001 ( tied_net ) ,
    .se30001 ( scan_en_net ) , .si30001 ( chain_net ) ,
    .cond30001 ( cond_in ) , .rst30001 ( rst_in ) , .clk30001 ( clk_in ) ,
    .q30001 ( q_out ) , .so30001 ( chain_tail ) ) ;
endmodule

module demo_soc ( test_se, chain_head, cond_in, rst_in, clk_in, q_out,
  chain_tail );
  input test_se, chain_head, cond_in, rst_in, clk_in;
  output q_out, chain_tail;
  demo_core core_inst ( .test_se ( test_se ) ,
    .chain_head ( chain_head ) , .cond_in ( cond_in ) , .rst_in ( rst_in ) ,
    .clk_in ( clk_in ) , .q_out ( q_out ) , .chain_tail ( chain_tail ) ) ;
endmodule

module tb_top ( test_se, chain_head, cond_in, rst_in, clk_in, q_out,
  chain_tail );
  input test_se, chain_head, cond_in, rst_in, clk_in;
  output q_out, chain_tail;
  demo_soc core ( .test_se ( test_se ) , .chain_head ( chain_head ) ,
    .cond_in ( cond_in ) , .rst_in ( rst_in ) , .clk_in ( clk_in ) ,
    .q_out ( q_out ) , .chain_tail ( chain_tail ) ) ;
endmodule
"""


@pytest.fixture(scope="module")
def netlist():
    return parse_verilog(NETLIST)


@pytest.fixture(scope="module")
def conn(netlist):
    return ConnectivityModel(netlist)


@pytest.fixture(scope="module")
def mapper(conn):
    return FaultMapper(conn)


# ---------------------------------------------------------------------------
# Test 1 -- with the netlist, the answer is SCAN, quoting the pins
# ---------------------------------------------------------------------------
def test_1_scan_verdict_is_derived_from_the_instantiation(mapper, conn):
    status = scan_status.determine_scan_status(TARGET, mapper, conn)

    assert status.verdict == scan_status.SCAN
    assert status.answer().startswith("SCAN")
    assert status.cell_type == "vendorcell_a1"
    assert status.module == "demo_flop_xover"


def test_1_quotes_the_si_ssb_and_so_pins_as_evidence(mapper, conn):
    status = scan_status.determine_scan_status(TARGET, mapper, conn)

    assert status.scan_in == ("si", "scan_in_net")
    assert status.shift_enable == ("ssb", "shift_en_net")
    assert status.scan_out == ("so", "scan_out_net")
    # The evidence must be quotable text read from the file, not a paraphrase.
    assert ".si ( scan_in_net )" in status.instantiation
    assert ".ssb ( shift_en_net )" in status.instantiation
    assert ".so ( scan_out_net )" in status.instantiation
    assert status.line_number is not None


def test_1_reports_the_three_corroborating_checks(mapper, conn):
    status = scan_status.determine_scan_status(TARGET, mapper, conn)

    assert set(status.corroboration) >= {"shift_enable", "scan_out", "scan_in"}
    # Scan-in is driven by real logic, so the cell is chain-connected.
    assert status.chain_connected is True
    assert "scan_en_net" in status.corroboration["shift_enable"]
    assert "port" in status.corroboration["scan_out"]


def test_1_duplicate_leaf_names_resolve_through_the_parent_module(mapper):
    """The right instance is picked among duplicates, not the first one."""
    leaf_matches = mapper._by_name["state_out_reg"]
    assert len(leaf_matches) > 1, "the fixture must exercise ambiguity"

    mapping = mapper.map_object(TARGET)
    assert mapping.confidence is MappingConfidence.HIGH
    assert mapping.module_name == "demo_flop_xover"
    assert mapping.cell_type == "vendorcell_a1"


# ---------------------------------------------------------------------------
# Test 2 -- without the netlist, the answer is exactly "Unresolved"
# ---------------------------------------------------------------------------
def test_2_no_netlist_yields_the_exact_unresolved_answer():
    result = investigate.scan_status(None, TARGET)

    assert result["verdict"] == "unresolved"
    assert result["answer"] == (
        "Unresolved - scan status cannot be determined without netlist pin "
        "evidence.")


def test_2_does_not_guess_non_scan_without_pin_evidence():
    result = investigate.scan_status(None, TARGET)
    blob = " ".join([result["answer"]] + result["evidence"]
                    + result["blockers"]).lower()

    assert "non-scan" not in blob
    assert "non_scan" not in blob
    assert result["verdict"] != "non_scan"


def test_2_unmapped_object_is_unresolved_even_with_a_netlist(mapper, conn):
    """A netlist that does not contain the object is still no evidence."""
    status = scan_status.determine_scan_status(
        "/some/design/that/is/not/here/u_missing_reg", mapper, conn)

    assert status.verdict == scan_status.UNRESOLVED
    assert status.answer() == scan_status.UNRESOLVED_ANSWER
    assert any("mapping failure" in b or "did not map" in b
               for b in status.blockers)


def _unmapped_result():
    """Build the exact fault-table row from the incident: all zeros, '-', 'N'."""
    fault = FaultRecord(
        raw_text=f"AU 0 {TARGET}/d",
        line_number=1,
        fault_object=f"{TARGET}/d",
        normalized_object=normalize_object(f"{TARGET}/d"),
        fault_class=FaultClass.AU,
        raw_class_token="AU",
        fault_type="0",
    )
    empty = parse_verilog("module empty_top ( a ); input a; endmodule")
    engine = RootCauseEngine(ConnectivityModel(empty),
                             FaultMapper(ConnectivityModel(empty)), [])
    return empty, fault, engine.analyze_fault(fault)


def test_2_unmapped_row_reports_null_not_zero():
    _, _, r = _unmapped_result()

    assert r.mapping.confidence is MappingConfidence.UNRESOLVED
    assert r.connectivity_known is False
    # The bug in one line: these used to be 0 and "no".
    assert r.fan_in_count is None
    assert r.fan_out_count is None
    assert r.scan_boundary_state == "unknown"


def test_2_unmapped_row_is_null_in_every_renderer():
    empty, fault, r = _unmapped_result()
    report = build_report(empty, [fault], [], [])

    row = csv_report.render_rows(report)[0]
    assert row["fan_in_count"] == ""
    assert row["fan_out_count"] == ""
    assert row["scan_boundary_involved"] == "unknown"

    md = markdown_report._fault_row(r)
    assert "| NULL | NULL |" in md
    assert "| unknown |" in md

    payload = build_user_payload(report)
    line = next(ln for ln in payload.splitlines()
                if ln.startswith(r.fault.fault_object))
    assert "| NULL | NULL |" in line
    assert line.strip().endswith(r.root_cause.value)
    assert "| unknown |" in line
    # And the payload must tell the model what NULL/unknown mean.
    assert "NULL is not zero and unknown is not 'no'" in payload


# ---------------------------------------------------------------------------
# Tied-constant reclassification (B2)
# ---------------------------------------------------------------------------
def test_tie_driver_is_resolved_across_four_hierarchy_levels(conn):
    resolution = conn.resolve_driver("demo_flop_xover", "d_in")

    assert resolution is not None
    assert resolution.is_tie is True
    assert resolution.instance.name == "u_tie_hi"
    assert resolution.instance.cell_type == "TIEHIX1"
    assert resolution.tie_value == "1"
    assert resolution.net == "tied_net"
    assert resolution.levels >= 4, resolution.trace


def test_tied_site_is_classified_tied_constant_not_scan_or_observability(
        conn, mapper):
    fault = FaultRecord(
        raw_text=f"AU 0 {TARGET}/d",
        line_number=1,
        fault_object=f"{TARGET}/d",
        normalized_object=normalize_object(f"{TARGET}/d"),
        fault_class=FaultClass.AU,
        raw_class_token="AU",
        fault_type="0",
    )
    result = RootCauseEngine(conn, mapper, []).analyze_fault(fault)

    assert result.root_cause is RootCause.TIED_CONSTANT
    assert result.root_cause is not RootCause.UNRESOLVED_CONNECTIVITY
    assert result.root_cause is not RootCause.OTHER_STRUCTURAL
    assert result.driver_resolution.instance.name == "u_tie_hi"
    assert "non-actionable" in " ".join(result.inferred_conclusions).lower()


def test_tie_cell_is_detected_structurally_not_only_by_name(conn):
    """An output-only cell is a constant source whatever it is called."""
    from atpg_coverage_debug_agent.analysis.connectivity import is_tie_cell

    netlist = parse_verilog(
        "module m ( o ); output o; wire n;"
        " weird_vendor_cell u_const ( .o ( n ) ) ;"
        " BUFX2 u_buf ( .a ( n ) , .o ( o ) ) ; endmodule")
    inst = netlist.modules["m"].instances["u_const"]

    assert is_tie_cell(inst, netlist) is True
    assert is_tie_cell(netlist.modules["m"].instances["u_buf"], netlist) is False


def test_a_cell_with_inputs_is_not_a_tie_however_it_is_named(conn):
    """Structure outranks naming: consuming a value rules out a constant."""
    from atpg_coverage_debug_agent.analysis.connectivity import is_tie_cell

    netlist = parse_verilog(
        "module m ( o ); output o; wire n;"
        # Named like a tie, but it has a real input pin, so it is not one.
        " and2_tie_hold u_trap ( .a ( n ) , .o ( o ) ) ;"
        " TIEHIX1 u_real ( .o ( n ) ) ; endmodule")

    trap = netlist.modules["m"].instances["u_trap"]
    assert is_tie_cell(trap, netlist) is False
    # The genuine output-only tie next to it is still found.
    assert is_tie_cell(netlist.modules["m"].instances["u_real"], netlist) is True


def test_naming_still_rescues_a_tie_with_no_usable_pin_evidence():
    """When structure is silent, the cell type is the only evidence there is."""
    from atpg_coverage_debug_agent.analysis.connectivity import is_tie_cell

    netlist = parse_verilog(
        "module m ( o ); output o; TIELOX1 u_pinless ( ) ; endmodule")
    inst = netlist.modules["m"].instances["u_pinless"]

    assert not inst.pins, "the fixture must leave structure with nothing to say"
    assert is_tie_cell(inst, netlist) is True


# ---------------------------------------------------------------------------
# Unresolved mappings are explained, not silently zeroed (B3)
# ---------------------------------------------------------------------------
def test_unresolved_mappings_are_attributed_to_a_cause(netlist, conn, mapper):
    from atpg_coverage_debug_agent.analysis import unresolved

    engine = RootCauseEngine(conn, mapper, [])
    missing = FaultRecord(
        raw_text="AU 0 /nowhere/at/all/u_ghost_reg/d",
        line_number=1,
        fault_object="/nowhere/at/all/u_ghost_reg/d",
        normalized_object=normalize_object("/nowhere/at/all/u_ghost_reg/d"),
        fault_class=FaultClass.AU,
        raw_class_token="AU",
        fault_type="0",
    )
    absent = FaultRecord(
        raw_text=f"AU 0 {TARGET}/../u_missing_model/o",
        line_number=2,
        fault_object=("/core/core_inst/clkblk/bootfsm/"
                      "u_missing_model/o"),
        normalized_object=normalize_object(
            "/core/core_inst/clkblk/bootfsm/u_missing_model/o"),
        fault_class=FaultClass.AU,
        raw_class_token="AU",
        fault_type="0",
    )
    results = [engine.analyze_fault(missing), engine.analyze_fault(absent)]

    diagnosis = unresolved.diagnose_unresolved(results, netlist)

    assert diagnosis.unresolved == 2
    assert diagnosis.by_cause.get(unresolved.OUTSIDE_SCOPE) == 1
    assert diagnosis.by_cause.get(unresolved.ABSENT_LEAF) == 1
    # The note must say UNKNOWN, never zero.
    assert "UNKNOWN, not zero" in diagnosis.note
    # Samples are verbatim so they resolve when pasted back into a tool.
    quoted = [s for g in diagnosis.groups for s in g.samples]
    assert "/nowhere/at/all/u_ghost_reg/d" in quoted


def test_diagnose_unresolved_tool_refuses_to_guess_without_a_netlist():
    result = investigate.run_tool(
        "diagnose_unresolved", {}, fault_results=[], constraints=[],
        netlist=None)

    assert "error" in result
    assert "cannot be attributed" in result["error"]


# ---------------------------------------------------------------------------
# The offline analysis carries the evidence into the summary page
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tied_report(netlist):
    """An offline analysis of the fixture, with one tied and one unmapped fault."""
    from atpg_coverage_debug_agent.analysis.summarizer import build_report

    def _fault(obj: str, line: int) -> FaultRecord:
        return FaultRecord(
            raw_text=f"AU 0 {obj}", line_number=line, fault_object=obj,
            normalized_object=normalize_object(obj),
            fault_class=FaultClass.AU, raw_class_token="AU", fault_type="0")

    faults = [
        _fault(f"{TARGET}/d", 1),
        _fault("/nowhere/at/all/u_ghost_reg/d", 2),
    ]
    return build_report(netlist, faults, [], [])


def test_offline_analysis_records_scan_and_tie_evidence(tied_report):
    tied = next(r for r in tied_report.fault_results
                if r.root_cause is RootCause.TIED_CONSTANT)

    assert tied.scan_cell_state == "scan"
    assert ".ssb ( shift_en_net )" in tied.scan_evidence
    assert tied.tie_driver["instance"] == "u_tie_hi"
    assert tied.tie_driver["value"] == "1"
    assert tied.tie_driver["levels"] >= 4


def test_summary_separates_actionable_loss_from_artefacts(tied_report):
    s = tied_report.summary

    assert s.coverage_loss_count == 2
    assert s.mapped_count == 1
    assert s.unmapped_count == 1
    assert s.tied_constant_count == 1
    # One row is unmapped and the other is a tied constant, so nothing here is
    # actionable -- the number a priority ranking may be built on is zero.
    assert s.actionable_loss_count == 0
    assert s.scan_evidence_counts.get("scan") == 1
    assert s.scan_evidence_counts.get("unknown") == 1
    assert s.unresolved_causes.get("outside_scope") == 1


def test_summary_page_shows_the_evidence_breakdown(tied_report):
    from atpg_coverage_debug_agent.reporting.html_report import (
        build_html_report)

    html = build_html_report(tied_report, design_name="fixture")

    assert "3. Evidence Quality" in html
    assert "Coverage-loss faults by evidence basis" in html
    assert "Actionable coverage loss" in html
    assert "u_tie_hi" in html
    assert "TIEHIX1" in html
    assert "expected and non-actionable" in html
    assert "unknown</b>, not zero" in html


def test_markdown_and_llm_payload_carry_the_same_breakdown(tied_report):
    from atpg_coverage_debug_agent.reporting.markdown_report import (
        render_markdown)

    md = render_markdown(tied_report)
    assert "## Evidence Quality" in md
    assert "Actionable coverage loss" in md
    assert "Constant drivers holding fault sites" in md

    payload = build_user_payload(tied_report)
    assert "## Evidence Basis of the Coverage Loss" in payload
    assert "connectivity is UNKNOWN for these, not zero" in payload
    assert "is NOT evidence of non-scan logic" in payload


def test_saved_report_keeps_the_evidence_breakdown(tied_report, tmp_path):
    from atpg_coverage_debug_agent.reporting.session_report import (
        load_report, save_report)

    path = str(tmp_path / "report.json")
    save_report(tied_report, path)
    reloaded = load_report(path)

    assert reloaded.summary.tied_constant_count == 1
    assert reloaded.summary.unmapped_count == 1
    assert reloaded.summary.actionable_loss_count == 0
    assert reloaded.summary.unresolved_causes == tied_report.summary.unresolved_causes
    tied = next(r for r in reloaded.fault_results if r.tie_driver)
    assert tied.tie_driver["instance"] == "u_tie_hi"
    assert tied.scan_cell_state == "scan"


# ---------------------------------------------------------------------------
# The system prompt must keep the rules that prevent the original error
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("required", [
    "Unresolved - scan status cannot be determined without netlist pin",
    "Step 5a Resolve real drivers before assigning any root cause",
    "PRECEDENCE: before applying any UC/UO/AU rule below",
    "SELF-CHECK before emitting any scan-status or root-cause claim",
    "means the extractor FAILED TO MAP the object",
])
def test_system_prompt_keeps_the_guardrails(required):
    assert required in SYSTEM_PROMPT


def test_system_prompt_no_longer_allows_a_naming_basis_for_scan_claims():
    assert "strong naming/library basis" not in SYSTEM_PROMPT
    assert "Naming basis is" in SYSTEM_PROMPT
