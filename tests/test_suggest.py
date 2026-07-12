"""Tests for the suggest_test_points deterministic recommender + tool."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.skills.manager import SkillManager


def test_suggest_returns_ranked_actions(sample_netlist_path, sample_faults_path,
                                        sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    out = investigate.suggest_test_points(rep.fault_results, limit=100)
    assert out["total"] >= 1
    sugg = out["suggestions"]
    # ranked highest-impact first
    scores = [s["score"] for s in sugg]
    assert scores == sorted(scores, reverse=True)
    for s in sugg:
        assert s["suggested_action"]
        assert s["kind"] in ("observability", "controllability", "constraint",
                             "scan", "other")


def test_suggest_focus_filter(sample_netlist_path, sample_faults_path,
                              sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    out = investigate.suggest_test_points(rep.fault_results, focus="observability")
    assert all(s["kind"] == "observability" for s in out["suggestions"])


def test_suggest_via_run_tool_and_skill(sample_netlist_path, sample_faults_path,
                                        sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    out = investigate.run_tool(
        "suggest_test_points", {"limit": 3}, fault_results=rep.fault_results,
        constraints=rep.constraints, netlist=rep.netlist)
    assert out["returned"] <= 3
    assert "suggest_test_points" in {s.skill_id for s in SkillManager().skills}
