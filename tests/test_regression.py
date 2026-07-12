"""Tests for the regression diff core and its exposure as agent tools."""

from __future__ import annotations

from atpg_coverage_debug_agent.analysis import investigate, regression
from atpg_coverage_debug_agent.app import run_analysis


def _faults(objs, cls="UO", rc="constraint_induced_observability_loss"):
    return [{"fault_object": o, "fault_class": cls, "root_cause": rc,
             "instance": o.split("/")[0]} for o in objs]


def test_diff_classifies_regressed_fixed_changed():
    base = _faults(["a/1", "b/2"])
    cur = _faults(["b/2", "c/3"])  # a/1 fixed, c/3 regressed
    d = regression.diff(base, cur)
    assert d["counts"]["regressed"] == 1
    assert d["counts"]["fixed"] == 1
    assert d["counts"]["net_delta"] == 0
    assert d["regressed"][0]["fault_object"] == "c/3"
    assert d["fixed"][0]["fault_object"] == "a/1"


def test_diff_detects_class_change():
    base = _faults(["a/1"], cls="UO")
    cur = _faults(["a/1"], cls="AU")
    d = regression.diff(base, cur)
    assert d["counts"]["changed"] == 1
    assert d["changed"][0]["base_class"] == "UO"
    assert d["changed"][0]["new_class"] == "AU"


def test_summary_has_class_deltas():
    base = _faults(["a/1", "b/2"])
    cur = _faults(["a/1"])
    s = regression.summary(
        base, cur, {"class_counts": {"UO": 2}}, {"class_counts": {"UO": 1}},
        label="prev.json")
    assert s["baseline_label"] == "prev.json"
    assert s["counts"]["fixed"] == 1
    assert s["class_deltas"]["UO"]["delta"] == -1


def test_run_tool_regression_requires_compare(sample_netlist_path,
                                              sample_faults_path,
                                              sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    # Without a compare payload the regression tools return an error.
    out = investigate.run_tool(
        "regression_summary", {}, fault_results=rep.fault_results,
        constraints=rep.constraints, netlist=rep.netlist)
    assert "error" in out


def test_run_tool_regression_with_compare(sample_netlist_path,
                                          sample_faults_path,
                                          sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    # Baseline = same report minus its first fault, so exactly one regression.
    baseline_faults = [investigate.serialize_fault_result(fr)
                       for fr in rep.fault_results[1:]]
    compare = {"label": "base", "faults": baseline_faults, "summary": {},
               "constraints": []}
    summ = investigate.run_tool(
        "regression_summary", {}, fault_results=rep.fault_results,
        constraints=rep.constraints, netlist=rep.netlist, compare=compare)
    assert summ["counts"]["regressed"] == 1

    regressed = investigate.run_tool(
        "list_regressed", {"limit": 5}, fault_results=rep.fault_results,
        constraints=rep.constraints, netlist=rep.netlist, compare=compare)
    assert regressed["total"] == 1
    assert regressed["faults"][0]["fault_object"] == \
        rep.fault_results[0].fault.fault_object


def test_regression_skills_registered():
    from atpg_coverage_debug_agent.skills.manager import SkillManager
    ids = {s.skill_id for s in SkillManager().skills}
    for name in ("regression_summary", "list_regressed", "list_fixed",
                 "list_changed"):
        assert name in ids


def test_serialize_report_for_compare(sample_netlist_path, sample_faults_path,
                                      sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    payload = investigate.serialize_report_for_compare(
        rep.fault_results, rep.summary, rep.constraints, label="x")
    assert payload["label"] == "x"
    assert len(payload["faults"]) == len(rep.fault_results)
    assert "class_counts" in payload["summary"]
