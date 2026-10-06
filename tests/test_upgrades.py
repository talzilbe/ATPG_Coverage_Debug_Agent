"""New-user GUI aids and the analysis upgrades (measured evidence, fix
history, SEQ/BB attribution, evidence tiers, the analysis cache)."""

from __future__ import annotations

import gzip
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from atpg_coverage_debug_agent.app import run_analysis  # noqa: E402
from atpg_coverage_debug_agent.analysis import fix_history  # noqa: E402
from atpg_coverage_debug_agent.analysis.analysis_cache import (  # noqa: E402
    AnalysisCache,
)
from atpg_coverage_debug_agent.analysis.attribution import (  # noqa: E402
    Attributor, attribute_black_boxes, attribute_sequential_sources,
)
from atpg_coverage_debug_agent.analysis.connectivity import (  # noqa: E402
    ConnectivityModel,
)
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper  # noqa: E402
from atpg_coverage_debug_agent.analysis.report_edit import (  # noqa: E402
    apply_exclusions,
)
from atpg_coverage_debug_agent.analysis.root_cause import (  # noqa: E402
    RootCauseEngine,
)
from atpg_coverage_debug_agent.analysis.tool_evidence import (  # noqa: E402
    apply_tool_evidence, load_tool_evidence, measured_verdict,
)
from atpg_coverage_debug_agent.config.settings import AppSettings  # noqa: E402
from atpg_coverage_debug_agent.gui import input_check  # noqa: E402
from atpg_coverage_debug_agent.parser.fault_parser import (  # noqa: E402
    parse_fault_list,
)
from atpg_coverage_debug_agent.parser.tessent_reports import (  # noqa: E402
    parse_analyze_fault, parse_report_statistics,
)
from atpg_coverage_debug_agent.parser.verilog_parser import (  # noqa: E402
    parse_verilog,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = tuple(os.path.join(ROOT, "sample_data", n) for n in (
    "demo_netlist.v", "demo_faults.mtfi", "demo_constraints.do"))

STATS_TEXT = """
   Statistics Report
   Stuck-at Faults
   ---------------------------------------------  ----------------
   Fault Classes                                    #faults
   ---------------------------------------------  ----------------
   FU (full)                                          1000
   ---------------------------------------------  ----------------
   DS (det_simulation)                                 800 (80.00%)
   AU (atpg_untestable)                                 60 ( 6.00%)
     TC (tied_cells)                                    40 ( 4.00%)
       /top/fifo_blk/uf_tdr_out_inter_reg (12) T1       38 ( 3.80%)
       (Individual tied cells below threshold)           2 ( 0.20%)
     PC (pin_constraints)                               20 ( 2.00%)
   UO (unobserved)                                      30 ( 3.00%)
     AAB (atpg_abort)                                   30 ( 3.00%)
   ---------------------------------------------  ----------------
   Coverage
   ------------------------------
     test_coverage                                   93.50%
     fault_coverage                                  80.00%
     atpg_effectiveness                              99.10%
"""

AF_TEXT = """\
//  ---------------------------------------------------------------------------
//  Fault analysis for /top/crypto_blk/uc_m0 (4310693) output Y stuck at 0
//  ---------------------------------------------------------------------------
//  Current fault classification = UO.AAB
//  Fault site sequential depth: Control_0 = 0, Control_1 = 0, Observe = 0.
//  Analyze if the fault can be activated ...
//      The fault site was set to 1 successfully (data in parallel_pattern 0).
//  Search potential observation point...
//    7 potential observation points were identified:
//  Analyze if the fault can be propagated to any observation point...
//      Fault was not observed successfully (ATPG status = abort).
//  Analysis summary:
//    Increasing the abort limit may help to detect the fault.
//  ---------------------------------------------------------------------------
//  Fault analysis for /top/dead_blk/ud_head (77) output Y stuck at 1
//  Current fault classification = UO.AAB
//  Fault site sequential depth: Control_0 = 0, Control_1 = 0, Observe = 0.
//      The fault site was set to 0 successfully (data in parallel_pattern 0).
//    No potential observation point was identified.
"""


@pytest.fixture(scope="module")
def demo_report():
    if not all(os.path.isfile(p) for p in DEMO):
        pytest.skip("demo data not present")
    return run_analysis(*DEMO)


# ---------------------------------------------------------------------------
# Input checks and recent inputs
# ---------------------------------------------------------------------------
def test_input_checks_recognise_the_demo_files(tmp_path):
    assert input_check.check_netlist(DEMO[0]).state == input_check.OK
    faults = input_check.check_faults(DEMO[1])
    assert faults.state == input_check.OK and "MTFI" in faults.message
    assert input_check.check_netlist("").state == input_check.ERROR
    assert input_check.check_netlist(str(tmp_path / "nope.v")).state == \
        input_check.ERROR
    assert input_check.check_constraints("").state == input_check.EMPTY


def test_input_checks_read_compressed_files(tmp_path):
    gz = tmp_path / "n.v.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write("module top (a);\n input a;\nendmodule\n")
    result = input_check.check_netlist(str(gz))
    assert result.state == input_check.OK and "gzip" in result.message


def test_a_text_file_that_is_not_verilog_is_flagged(tmp_path):
    bad = tmp_path / "x.v"
    bad.write_text("hello\n")
    assert input_check.check_netlist(str(bad)).state == input_check.WARN


def test_tool_report_input_is_recognised(tmp_path):
    stats = tmp_path / "stats.log"
    stats.write_text(STATS_TEXT)
    assert input_check.check_tool_reports(str(stats)).state == input_check.OK
    assert input_check.check_tool_reports(str(tmp_path)).state == input_check.OK
    assert input_check.check_tool_reports("").state == input_check.EMPTY


def test_recent_inputs_are_deduplicated_and_capped():
    s = AppSettings()
    for i in range(12):
        s.remember_inputs({"netlist": f"n{i}", "faults": "f"})
    s.remember_inputs({"netlist": "n5", "faults": "f"})
    assert len(s.recent_inputs) == 8
    assert s.recent_inputs[0]["netlist"] == "n5"
    assert [e["netlist"] for e in s.recent_inputs].count("n5") == 1


# ---------------------------------------------------------------------------
# Tessent reports: parsing, cross-check, measured override
# ---------------------------------------------------------------------------
def test_report_statistics_is_parsed():
    rep = parse_report_statistics(STATS_TEXT)
    assert rep.classes["AU"] == 60 and rep.classes["DS"] == 800
    assert rep.subclasses["AU.TC"] == 40 and rep.subclasses["UO.AAB"] == 30
    assert rep.metrics["test_coverage"] == 93.5
    assert rep.tie_sources[0].value == "T1"
    assert rep.tie_sources[0].count == 38


def test_analyze_fault_entries_are_parsed_and_judged():
    entries = parse_analyze_fault(AF_TEXT)
    assert len(entries) == 2
    first, second = entries
    assert first.fault_object == "/top/crypto_blk/uc_m0/Y"
    assert first.activatable is True and first.observation_points == 7
    assert first.status == "abort"
    assert measured_verdict(first) == "reconvergent_complexity"
    assert second.observation_points == 0
    assert measured_verdict(second) == "hard_observability_gap"


def test_measured_evidence_is_compared_and_overrides(tmp_path, demo_report):
    (tmp_path / "stats.log").write_text(STATS_TEXT)
    (tmp_path / "af.log").write_text(AF_TEXT)
    evidence = load_tool_evidence(str(tmp_path))
    assert evidence.statistics and len(evidence.analyses) == 2
    apply_tool_evidence(demo_report, evidence)
    assert demo_report.tool_evidence is evidence
    assert any(r["metric"] == "test_coverage" for r in evidence.metric_check)
    assert any(r["class"] == "AU.TC" for r in evidence.class_check)
    assert evidence.tie_check and evidence.tie_check[0]["found_by_tracing"]
    measured = evidence.measured_categories.get("UO.AAB")
    assert measured and measured["samples"] == 2
    cat = next(c for c in demo_report.selected_categories
               if c.subclass_id == "UO.AAB")
    assert cat.reachability.measured
    assert cat.reachability.note.startswith("[measured by analyze_fault")
    assert cat.reachability.as_dict()["source"] == "tool_report"


def test_measured_evidence_survives_save_and_load(tmp_path, demo_report):
    from atpg_coverage_debug_agent.reporting.session_report import (
        load_report, save_report,
    )
    (tmp_path / "stats.log").write_text(STATS_TEXT)
    apply_tool_evidence(demo_report, load_tool_evidence(str(tmp_path)))
    path = tmp_path / "s.json"
    save_report(demo_report, str(path), dump_categories=False)
    loaded = load_report(str(path))
    assert loaded.tool_evidence is not None
    assert loaded.tool_evidence.metric_check


# ---------------------------------------------------------------------------
# Fix outcomes and the history ledger
# ---------------------------------------------------------------------------
def test_fix_outcomes_measure_each_category(demo_report, tmp_path):
    top = demo_report.recommendations[0].subclass_id
    ids = [fr.fault.fault_object for fr in demo_report.fault_results
           if fr.fault.dotted_class == top][:3]
    after = apply_exclusions(demo_report, excluded_ids=ids)
    outcomes = fix_history.evaluate_fix_outcomes(demo_report, after)
    row = next(o for o in outcomes if o.subclass == top)
    assert row.recovered >= len(set(ids)) - 0
    assert row.after == row.before - row.recovered - row.moved


def test_history_promotes_a_proven_fix(demo_report, tmp_path):
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )
    stats = demo_report.relevant_statistics or demo_report.statistics
    base = build_recommendations(stats, demo_report.selected_categories)
    sub = base[0].subclass_id
    same = [r for r in base if r.subclass_id == sub]
    if len(same) < 2:
        pytest.skip("top category has a single fix")
    loser = same[-1]
    outcome = fix_history.FixOutcome(
        rank=loser.rank, subclass=sub, fix_id=loser.fix.fix_id,
        title=loser.title, before=10, after=1, recovered=9, moved=0, new=0)
    assert fix_history.record_outcomes([outcome], design="demo") == 1
    again = build_recommendations(stats, demo_report.selected_categories)
    ranked = [r.fix.fix_id for r in again if r.subclass_id == sub]
    assert ranked.index(loser.fix.fix_id) < [
        r.fix.fix_id for r in same].index(loser.fix.fix_id)
    promoted = next(r for r in again if r.fix.fix_id == loser.fix.fix_id
                    and r.subclass_id == sub)
    assert promoted.history["verdict"] == "proven"
    assert any(e.startswith("[fix_history]") for e in promoted.evidence)


# ---------------------------------------------------------------------------
# AU.SEQ / AU.BB attribution
# ---------------------------------------------------------------------------
SEQ_BB = """
module blk (clk, a, o1, o2);
  input clk, a;
  output o1, o2;
  wire n1, n2, n3, m1;
  BUF      u_s_head ( .A(a), .Y(n1) );
  DFF_nsff u_s_ns ( .D(n1), .CK(clk), .Q(n2) );
  SDFF     u_s_scan ( .D(n2), .CK(clk), .Q(o1) );
  BUF      u_b_head ( .A(a), .Y(n3) );
  SRAM64X8 u_ram ( .A0(n3), .A1(n3), .CK(clk), .Q0(m1) );
  SDFF     u_b_scan ( .D(m1), .CK(clk), .Q(o2) );
endmodule
"""


def _results(text):
    netlist = parse_verilog(SEQ_BB)
    conn = ConnectivityModel(netlist)
    engine = RootCauseEngine(conn, FaultMapper(conn), [])
    faults, _ = parse_fault_list(text)
    return conn, [engine.analyze_fault(f) for f in faults if f.is_coverage_loss]


def test_au_seq_is_traced_to_unscanned_state():
    conn, results = _results("AU.SEQ 1 blk/u_s_head/Y\n")
    att = attribute_sequential_sources(results, Attributor(conn))
    assert att.verdict == "non_scan_sequential"
    assert att.tie_sources[0].driver == "u_s_ns"
    assert att.preferred_fix_ids == ["seq_observe_point"]


def test_au_seq_next_to_a_memory_points_at_ram_drc():
    conn, results = _results("AU.SEQ 1 blk/u_b_head/Y\n")
    att = attribute_sequential_sources(results, Attributor(conn))
    assert att.verdict == "memory_adjacent"
    assert att.preferred_fix_ids == ["seq_drc_check"]


def test_au_bb_ranks_the_unmodelled_macro():
    conn, results = _results("AU.BB 1 blk/u_b_head/Y\n")
    att = attribute_black_boxes(results, Attributor(conn))
    assert att.verdict == "black_box_module"
    assert att.tie_sources[0].cell_type == "SRAM64X8"
    assert "naming" in att.note


# ---------------------------------------------------------------------------
# The analysis cache
# ---------------------------------------------------------------------------
def test_a_second_run_reuses_mappings_with_identical_results(tmp_path,
                                                             monkeypatch):
    if not all(os.path.isfile(p) for p in DEMO):
        pytest.skip("demo data not present")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    first = run_analysis(*DEMO)
    second = run_analysis(DEMO[0], DEMO[1], None)
    assert first.sources["analysis_cache"]["reused_mappings"] == 0
    assert second.sources["analysis_cache"]["reused_mappings"] > 0
    third = run_analysis(*DEMO)
    assert [r.mapping.instance_name for r in third.fault_results] == \
        [r.mapping.instance_name for r in first.fault_results]
    assert [c.reachability.dominant if c.reachability else None
            for c in third.selected_categories] == \
        [c.reachability.dominant if c.reachability else None
         for c in first.selected_categories]


def test_the_cache_is_off_when_netlist_caching_is_off(monkeypatch):
    monkeypatch.setenv("ATPG_NETLIST_CACHE", "0")
    assert AnalysisCache.for_netlist(DEMO[0]) is None


# ---------------------------------------------------------------------------
# Evidence tiers in the Triage tab
# ---------------------------------------------------------------------------
def test_categories_carry_evidence_tiers(demo_report):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from atpg_coverage_debug_agent.gui.triage_panel import (
        TriagePanel, evidence_tiers,
    )
    tc = next(c for c in demo_report.selected_categories
              if c.subclass_id == "AU.TC")
    tiers = evidence_tiers(tc, demo_report)
    assert "fault_list" in tiers and "structural" in tiers
    panel = TriagePanel()
    panel.set_report(demo_report)
    col = panel.category_table.columnCount() - 1
    assert "fault list" in panel.category_table.item(0, col).text()
    html = panel._category_html("AU.TC")
    assert "structural estimate" in html
