"""Tests for the structural profiling of aborted-fault sites."""

from __future__ import annotations

import pytest

from atpg_coverage_debug_agent.analysis.attribution import Attributor
from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.analysis.reachability import (
    RECONVERGENCE_HIGH,
    SEQ_DEPTH_HIGH,
    SiteProfile,
    StructuralProfiler,
    classify_site,
    profile_categories,
    profile_faults,
)
from atpg_coverage_debug_agent.analysis.root_cause import RootCauseEngine
from atpg_coverage_debug_agent.parser.constraint_parser import parse_constraints
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog

# A design with one cone per structural situation the decision rules separate.
NETLIST = """
module blk (clk, i_gap, i_bn, i_ctl, i_rec, i_deep, i_open,
            o_gap, o_bn, o_ctl, o_rec, o_deep, o_a, o_b, o_c);
  input  clk, i_gap, i_bn, i_ctl, i_rec, i_deep, i_open;
  output o_gap, o_bn, o_ctl, o_rec, o_deep, o_a, o_b, o_c;
  wire   g1, b1, c0, cy, r0, ra, rb, m1, m2, m3, m4, m5, rmerge;
  wire   d0, d1, d2, d3, d4, d5, d6, d7, d8, op0;

  // 1. Nothing downstream can capture the effect: no scan cell at all.
  BUF   u_gap_head ( .A(i_gap), .Y(g1) );
  BUF   u_gap_tail ( .A(g1), .Y(o_gap) );

  // 2. Exactly one observation point: a narrow channel.
  BUF   u_bn_head ( .A(i_bn), .Y(b1) );
  SDFF  u_bn_scan ( .D(b1), .CK(clk), .Q(o_bn) );

  // 3. A constant reaches the site, so it cannot be activated.
  TIE0  u_ctl_tie ( .Y(c0) );
  AND2  u_ctl_head ( .A(c0), .B(i_ctl), .Y(cy) );
  SDFF  u_ctl_scan ( .D(cy), .CK(clk), .Q(o_ctl) );

  // 4. Paths fan out and re-merge repeatedly before any capture point.
  BUF   u_rec_head ( .A(i_rec), .Y(r0) );
  BUF   u_rec_a ( .A(r0), .Y(ra) );
  BUF   u_rec_b ( .A(r0), .Y(rb) );
  AND2  u_rec_m1 ( .A(ra), .B(rb), .Y(m1) );
  AND2  u_rec_m2 ( .A(ra), .B(rb), .Y(m2) );
  AND2  u_rec_m3 ( .A(ra), .B(rb), .Y(m3) );
  AND2  u_rec_m4 ( .A(ra), .B(rb), .Y(m4) );
  AND2  u_rec_m5 ( .A(ra), .B(rb), .Y(m5) );
  AND2  u_rec_join ( .A(m1), .B(m2), .Y(rmerge) );
  SDFF  u_rec_scan ( .D(rmerge), .CK(clk), .Q(o_rec) );

  // 5. A long chain of unscanned flops before anything can capture.
  BUF      u_deep_head ( .A(i_deep), .Y(d0) );
  DFF_nsff u_deep_1 ( .D(d0), .CK(clk), .Q(d1) );
  DFF_nsff u_deep_2 ( .D(d1), .CK(clk), .Q(d2) );
  DFF_nsff u_deep_3 ( .D(d2), .CK(clk), .Q(d3) );
  DFF_nsff u_deep_4 ( .D(d3), .CK(clk), .Q(d4) );
  DFF_nsff u_deep_5 ( .D(d4), .CK(clk), .Q(d5) );
  DFF_nsff u_deep_6 ( .D(d5), .CK(clk), .Q(d6) );
  DFF_nsff u_deep_7 ( .D(d6), .CK(clk), .Q(d7) );
  DFF_nsff u_deep_8 ( .D(d7), .CK(clk), .Q(d8) );
  SDFF     u_deep_scan ( .D(d8), .CK(clk), .Q(o_deep) );

  // 6. Plenty of independent observation points: structurally healthy.
  BUF   u_open_head ( .A(i_open), .Y(op0) );
  SDFF  u_open_a ( .D(op0), .CK(clk), .Q(o_a) );
  SDFF  u_open_b ( .D(op0), .CK(clk), .Q(o_b) );
  SDFF  u_open_c ( .D(op0), .CK(clk), .Q(o_c) );
endmodule
"""


def _analysed(fault_text, constraint_text=""):
    netlist = parse_verilog(NETLIST)
    faults, _ = parse_fault_list(fault_text)
    constraints, _ = parse_constraints(constraint_text)

    conn = ConnectivityModel(netlist)
    engine = RootCauseEngine(conn, FaultMapper(conn), constraints)
    results = [engine.analyze_fault(f) for f in faults if f.is_coverage_loss]
    return conn, constraints, results


def _profiler(conn, constraints=()):
    return StructuralProfiler(conn, Attributor(conn, constraints))


def _signature_of(conn, instance):
    return _profiler(conn).profile(instance).signature


# ---------------------------------------------------------------------------
# The decision rules, applied to measurements directly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile,expected", [
    (SiteProfile(sequential_depth=SEQ_DEPTH_HIGH, observation_points=4),
     "sequential_depth_explosion"),
    (SiteProfile(activatable=False, observation_points=4),
     "low_controllability"),
    (SiteProfile(activatable=True, observation_points=0),
     "hard_observability_gap"),
    (SiteProfile(activatable=True, observation_points=9,
                 reconvergence=RECONVERGENCE_HIGH),
     "reconvergent_complexity"),
    (SiteProfile(activatable=True, observation_points=1),
     "observability_bottleneck"),
    (SiteProfile(activatable=True, observation_points=6),
     "no_structural_blocker"),
])
def test_measurements_map_to_the_expected_verdict(profile, expected):
    assert classify_site(profile) == expected


def test_depth_outranks_every_other_signal():
    # A deep chain is the dominant problem even when the site also looks
    # uncontrollable: shortening the chain is what unblocks it.
    profile = SiteProfile(sequential_depth=SEQ_DEPTH_HIGH + 2,
                          activatable=False, observation_points=0,
                          reconvergence=20)
    assert classify_site(profile) == "sequential_depth_explosion"


# ---------------------------------------------------------------------------
# Measuring real structure
# ---------------------------------------------------------------------------
def test_cone_with_no_capture_point_is_an_observability_gap():
    conn, _, _ = _analysed("")
    profile = _profiler(conn).profile("u_gap_head")
    assert profile.observation_points == 0
    assert profile.signature == "hard_observability_gap"


def test_single_capture_point_is_a_bottleneck():
    conn, _, _ = _analysed("")
    profile = _profiler(conn).profile("u_bn_head")
    assert profile.observation_points == 1
    assert profile.signature == "observability_bottleneck"


def test_constant_upstream_makes_the_site_unactivatable():
    conn, _, _ = _analysed("")
    profile = _profiler(conn).profile("u_ctl_head")
    assert profile.activatable is False
    assert profile.signature == "low_controllability"


def test_repeated_re_merging_is_reported_as_reconvergence():
    conn, _, _ = _analysed("")
    profile = _profiler(conn).profile("u_rec_head")
    assert profile.observation_points >= 1
    assert profile.reconvergence >= RECONVERGENCE_HIGH
    assert profile.signature == "reconvergent_complexity"


def test_long_unscanned_chain_is_a_depth_problem():
    conn, _, _ = _analysed("")
    profile = _profiler(conn).profile("u_deep_head")
    assert profile.sequential_depth >= SEQ_DEPTH_HIGH
    assert profile.signature == "sequential_depth_explosion"


def test_healthy_cone_reports_no_structural_blocker():
    conn, _, _ = _analysed("")
    profile = _profiler(conn).profile("u_open_head")
    assert profile.observation_points == 3
    assert profile.signature == "no_structural_blocker"


def test_unknown_instance_yields_no_profile():
    conn, _, _ = _analysed("")
    assert _profiler(conn).profile("does_not_exist") is None


def test_profiles_are_memoised_per_instance():
    conn, _, _ = _analysed("")
    profiler = _profiler(conn)
    first = profiler.profile("u_bn_head")
    assert profiler.profile("u_bn_head") is first


# ---------------------------------------------------------------------------
# Category-level summary
# ---------------------------------------------------------------------------
def test_dominant_signature_is_reported_with_its_share():
    text = ("UO.AAB 1 blk/u_gap_head/Y\n" * 1
            + "UO.AAB 1 blk/u_bn_head/Y\n" * 4)
    conn, constraints, results = _analysed(text)
    outcome = profile_faults(results, _profiler(conn, constraints), "UO.AAB")

    assert outcome.profiled == 5
    assert outcome.dominant == "observability_bottleneck"
    assert outcome.dominant_share == pytest.approx(0.8)
    assert outcome.consensus is True
    assert "whole category" in outcome.note


def test_mixed_category_is_declared_mixed_rather_than_summarised_away():
    text = ("UO.AAB 1 blk/u_gap_head/Y\n"
            "UO.AAB 1 blk/u_bn_head/Y\n"
            "UO.AAB 1 blk/u_rec_head/Y\n")
    conn, constraints, results = _analysed(text)
    outcome = profile_faults(results, _profiler(conn, constraints), "UO.AAB")

    assert outcome.consensus is False
    assert "structurally mixed" in outcome.note
    assert len(outcome.signatures) == 3


def test_unlocatable_faults_are_counted_not_silently_dropped():
    conn, constraints, results = _analysed("UO.AAB 1 nowhere/absent/Y\n")
    outcome = profile_faults(results, _profiler(conn, constraints), "UO.AAB")

    assert outcome.analysed == 1
    assert outcome.profiled == 0
    assert outcome.unresolved_mapping == 1
    assert "None of the 1 fault(s)" in outcome.note
    assert outcome.dominant == ""


def test_samples_are_verbatim_fault_paths():
    conn, constraints, results = _analysed("UO.AAB 1 blk/u_bn_head/Y\n")
    outcome = profile_faults(results, _profiler(conn, constraints), "UO.AAB")
    assert outcome.samples["observability_bottleneck"] == ["blk/u_bn_head/Y"]


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------
def _selected_for(text, conn, results):
    from atpg_coverage_debug_agent.analysis.statistics import (
        compute_statistics,
        enrich_categories,
        select_categories,
    )

    faults = [r.fault for r in results]
    stats = compute_statistics(faults)
    return stats, enrich_categories(select_categories(stats), faults)


def test_reconvergence_promotes_a_design_bypass_over_more_abort_budget():
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )

    text = "\n".join(f"UO.AAB 1 blk/u_rec_head/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    stats, selected = _selected_for(text, conn, results)
    profile_categories(selected, results, conn, constraints)

    assert selected[0].reachability.dominant == "reconvergent_complexity"
    recs = build_recommendations(stats, selected)
    # Raising the abort limit is the cheaper action, but it does not help a
    # reconvergent structure, so the bypass must come first.
    assert recs[0].fix.fix_id == "aab_design_bypass"
    assert [r.fix.fix_id for r in recs].index("aab_design_bypass") < \
        [r.fix.fix_id for r in recs].index("aab_abort_limit")


def test_bottleneck_keeps_the_cheap_abort_limit_first():
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )

    text = "\n".join(f"UO.AAB 1 blk/u_bn_head/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    stats, selected = _selected_for(text, conn, results)
    profile_categories(selected, results, conn, constraints)

    assert selected[0].reachability.dominant == "observability_bottleneck"
    recs = build_recommendations(stats, selected)
    assert recs[0].fix.fix_id == "aab_abort_limit"


def test_only_aborted_categories_are_profiled():
    text = "\n".join(f"AU.TC 1 blk/u_bn_head/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    _, selected = _selected_for(text, conn, results)
    profile_categories(selected, results, conn, constraints)
    assert selected[0].reachability is None


def test_profiling_is_skipped_without_a_netlist():
    text = "\n".join(f"UO.AAB 1 blk/u_bn_head/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    _, selected = _selected_for(text, conn, results)
    profile_categories(selected, results, None, constraints)
    assert all(getattr(c, "reachability", None) is None for c in selected)


def test_profile_tool_reads_the_serialized_payload():
    from atpg_coverage_debug_agent.analysis import investigate
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )

    text = "\n".join(f"UO.AAB 1 blk/u_rec_head/Y{i}" for i in range(12))
    conn, constraints, results = _analysed(text)
    stats, selected = _selected_for(text, conn, results)
    profile_categories(selected, results, conn, constraints)
    payload = investigate.serialize_triage(
        stats, selected, build_recommendations(stats, selected))

    data = investigate.run_tool(
        "profile_fault_sites", {}, fault_results=[], constraints=[],
        netlist=None, triage=payload)
    assert data["categories"]
    entry = data["categories"][0]
    assert entry["dominant"] == "reconvergent_complexity"
    assert entry["signatures"][0]["samples"]
    assert "opposite fixes" in data["note"]
