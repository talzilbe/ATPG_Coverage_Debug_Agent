"""Tests for the deterministic investigation query core and skills."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.skills.manager import SkillManager


def _report(nl, fl, cn):
    return run_analysis(nl, fl, cn)


def test_list_faults_returns_matches(sample_netlist_path, sample_faults_path,
                                     sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    out = investigate.list_faults(rep.fault_results, limit=3)
    assert out["total_matched"] == len(rep.fault_results)
    assert out["returned"] <= 3
    assert all("fault_object" in row for row in out["faults"])


def test_list_faults_filter_by_class(sample_netlist_path, sample_faults_path,
                                     sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    out = investigate.list_faults(rep.fault_results, fault_class="UO")
    assert all(r["fault_class"] == "UO" for r in out["faults"])


def test_get_fault_detail_and_why_blocked(sample_netlist_path,
                                          sample_faults_path,
                                          sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    fo = rep.fault_results[0].fault.fault_object
    detail = investigate.get_fault_detail(rep.fault_results, fo)
    assert detail["total_matched"] >= 1
    assert detail["faults"][0]["fault_object"] == fo
    assert "evidence" in detail["faults"][0]

    wb = investigate.why_blocked(rep.fault_results, fo)
    assert wb["total_matched"] >= 1
    assert "verdict" in wb["faults"][0]


def test_get_fault_detail_requires_query(sample_netlist_path,
                                         sample_faults_path,
                                         sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    assert "error" in investigate.get_fault_detail(rep.fault_results, "")


def test_list_constraints(sample_netlist_path, sample_faults_path,
                          sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    out = investigate.list_constraints(rep.constraints)
    assert out["total_matched"] == len(rep.constraints)


def test_run_tool_dispatch(sample_netlist_path, sample_faults_path,
                           sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    out = investigate.run_tool(
        "list_faults", {"limit": 2}, fault_results=rep.fault_results,
        constraints=rep.constraints, netlist=rep.netlist)
    assert out["returned"] <= 2
    assert "error" in investigate.run_tool(
        "nope", {}, fault_results=rep.fault_results,
        constraints=rep.constraints, netlist=rep.netlist)


def test_export_and_rehydrate_roundtrip(sample_netlist_path, sample_faults_path,
                                        sample_constraints_path):
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    evidence = investigate.export_evidence(
        rep.fault_results, rep.constraints, rep.netlist)
    faults, constraints, adjacency = investigate.rehydrate(evidence)
    assert len(faults) == len(rep.fault_results)
    assert len(constraints) == len(rep.constraints)
    # Same query works on rehydrated objects (out-of-process parity).
    live = investigate.list_faults(rep.fault_results)["total_matched"]
    rehy = investigate.list_faults(faults)["total_matched"]
    assert live == rehy


def test_trace_path_adjacency_no_path():
    adj = {"A": ["B"], "B": ["C"]}
    hit = investigate.trace_path_adjacency(adj, "A", "C", max_depth=8)
    assert hit["found"] is True
    assert hit["path"][0] == "A" and hit["path"][-1] == "C"
    miss = investigate.trace_path_adjacency(adj, "C", "A", max_depth=8)
    assert miss["found"] is False


def test_investigative_skills_are_on_demand():
    mgr = SkillManager()
    ids = {s.skill_id for s in mgr.skills}
    for name in ("list_faults", "get_fault_detail", "why_blocked",
                 "list_constraints", "trace_path"):
        assert name in ids
    on_demand = {s.skill_id for s in mgr.skills if s.on_demand}
    assert "get_fault_detail" in on_demand


def test_on_demand_skills_skipped_in_bulk(sample_netlist_path,
                                          sample_faults_path,
                                          sample_constraints_path):
    from atpg_coverage_debug_agent.skills.base import AnalysisContext
    rep = _report(sample_netlist_path, sample_faults_path,
                  sample_constraints_path)
    mgr = SkillManager()
    ctx = AnalysisContext(
        netlist=rep.netlist, faults=rep.faults, constraints=rep.constraints,
        fault_results=rep.fault_results, pattern_groups=rep.pattern_groups,
        summary=rep.summary)
    results = mgr.run_all(ctx)
    ran_ids = {r.skill_id for r in results}
    assert "get_fault_detail" not in ran_ids  # on-demand tools not bulk-run
