"""Skills and tools as the agent sees them: honest, paged, one surface."""

import json
import os
from types import SimpleNamespace

import pytest

from atpg_coverage_debug_agent import mcp_server
from atpg_coverage_debug_agent.agent.debug_agent import (
    AgentConfig, DebugAgent, _serialize_skill_result)
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.skills.base import AnalysisContext, SkillResult
from atpg_coverage_debug_agent.skills.fault_cone_summary import (
    FaultConeSummarySkill)
from atpg_coverage_debug_agent.skills.manager import SkillManager
from atpg_coverage_debug_agent.skills.markdown_skill import (
    GUIDANCE_NOTE, guidance_sections, make_markdown_skill_class,
    split_front_matter)
from atpg_coverage_debug_agent.skills.scan_boundary import ScanBoundarySkill

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_HERE, "sample_data")


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(os.path.join(_SAMPLE, "demo_netlist.v"),
                        os.path.join(_SAMPLE, "demo_faults.mtfi"),
                        os.path.join(_SAMPLE, "demo_constraints.do"))


def _ctx(results):
    return AnalysisContext(netlist=None, faults=[], constraints=[],
                           fault_results=results, pattern_groups=[],
                           summary=None)


def _site(obj, state="unknown", known=True, boundary=False, fan_in=(),
          fan_out=(), evidence=""):
    return SimpleNamespace(
        fault=SimpleNamespace(fault_object=obj), scan_cell_state=state,
        scan_evidence=evidence, connectivity_known=known,
        scan_boundary_state=("yes" if boundary else "no") if known else
        "unknown", fan_in=list(fan_in), fan_out=list(fan_out),
        mapping=SimpleNamespace(module_name="CELL"), cell_type="CELL")


# ---------------------------------------------------------------------------
# Step 1: skills obey the same honesty rules as the agent
# ---------------------------------------------------------------------------
def test_scan_boundary_never_decides_from_names():
    # Names that the old regexes classified as non-scan / clock gate.
    results = [_site("top/u_jtag_tap_/sdff_reg", "scan"),
               _site("top/u_icg_clkgate/q", "scan"),
               _site("top/plain/a", "unknown"),
               _site("top/plain/b", "unknown")]
    skill = ScanBoundarySkill()
    out = skill.run(_ctx(results))
    titles = " ".join(f.title for f in out.findings)
    assert "non-scan" not in titles
    assert "2 loss site(s) with unresolved scan status" in titles
    unresolved = next(f for f in out.findings if "unresolved" in f.title)
    assert unresolved.confidence == "insufficient"
    assert "2 scan cell, 0 non-scan state element" in out.summary
    assert "2 unresolved" in out.summary


def test_a_gate_without_scan_pins_is_not_a_non_scan_cell():
    gates = [_site(f"top/g{i}", "non_scan",
                   evidence=f"AND2X1 g{i} (.A(a), .B(b), .Y(y));")
             for i in range(5)]
    out = ScanBoundarySkill().run(_ctx(gates))
    assert not any("non-scan" in f.title for f in out.findings)
    assert "5 combinational (not applicable)" in out.summary


def test_scan_boundary_reports_pin_evidence_and_marks_subsets():
    results = [_site(f"top/b/r{i}", "non_scan",
                     evidence="DFFX1 r (.D(a), .CK(c), .Q(q));")
               for i in range(12)]
    out = ScanBoundarySkill().run(_ctx(results))
    finding = out.findings[0]
    assert finding.confidence == "high"
    assert any("instantiation read" in e for e in finding.evidence)
    assert finding.affected_objects[-1] == "... (10 of 12 listed)"


def test_naming_hints_are_opt_in_and_labelled():
    results = [_site("top/u_icg_clkgate/a"), _site("top/u_clkgate/b")]
    skill = ScanBoundarySkill()
    assert not any("Naming" in f.title for f in skill.run(_ctx(results)).findings)
    skill.set_param("naming_hints", True)
    hint = next(f for f in skill.run(_ctx(results)).findings
                if "Naming" in f.title)
    assert hint.confidence == "low" and "not evidence" in hint.description


def test_cone_summary_keeps_unmapped_sites_out_of_zero_counts():
    results = ([_site(f"u{i}", known=False) for i in range(8)]
               + [_site("m1", fan_in=["x"], fan_out=["y"])])
    out = FaultConeSummarySkill().run(_ctx(results))
    titles = [f.title for f in out.findings]
    assert "8 site(s) did not map onto the netlist" in titles
    assert not any("zero fan-in" in t for t in titles)
    assert "1 mapped site(s)" in out.summary and "8 unmapped" in out.summary


def test_markdown_front_matter_is_parsed_not_shown(tmp_path):
    md = tmp_path / "my_guide.md"
    md.write_text("---\nname: my-guide\ndescription: 'How to read AU.TC'\n"
                  "---\n\n# My Guide\n\nIntro.\n\n## Where to look\nTies.\n\n"
                  "## Fixes\nTDR top-off.\n")
    cls = make_markdown_skill_class(str(md))
    assert cls.skill_id == "my_guide"          # stable: from the file name
    assert cls.description == "How to read AU.TC"
    assert cls.guidance is True
    finding = cls().run(_ctx([])).findings[0]
    assert "---" not in finding.description.splitlines()[2:]
    assert finding.description.startswith(GUIDANCE_NOTE)
    assert [s["title"] for s in guidance_sections(md.read_text())] == [
        "Overview", "Where to look", "Fixes"]
    fields, body = split_front_matter(md.read_text())
    assert fields["name"] == "my-guide" and body.lstrip().startswith("# My")


def test_repo_skill_md_description_is_not_the_front_matter_fence():
    fields, _ = split_front_matter(open(os.path.join(_HERE, "Skill.md")).read())
    assert fields.get("description", "").startswith("DFT/ATPG")


# ---------------------------------------------------------------------------
# Step 2: skills reach the agent the same way on every backend
# ---------------------------------------------------------------------------
@pytest.fixture
def skills_payload(demo_report, tmp_path):
    md = tmp_path / "guide.md"
    md.write_text("# Guide\n\n## Ties\nCheck TIEHI drivers first.\n")
    manager = SkillManager()
    manager.load_custom_skills(str(tmp_path))
    ctx = AnalysisContext(
        netlist=demo_report.netlist, faults=demo_report.faults,
        constraints=demo_report.constraints,
        fault_results=demo_report.fault_results,
        pattern_groups=demo_report.pattern_groups,
        summary=demo_report.summary)
    results = manager.run_all(ctx)
    return investigate.serialize_skills(results, manager.skills)


def _run(name, args, skills=None, **kw):
    return investigate.run_tool(name, args, fault_results=[], constraints=[],
                                netlist=None, skills=skills, **kw)


def test_skill_findings_lists_then_pages(skills_payload):
    listing = _run("skill_findings", {}, skills_payload)
    ids = {s["skill_id"] for s in listing["skills"]}
    assert {"scan_boundary", "fault_cone_summary"} <= ids
    assert "guide" not in ids, "guidance is read with read_guidance"
    page = _run("skill_findings", {"skill": "scan_boundary", "limit": 1},
                skills_payload)
    assert page["returned"] == 1 and "total_matched" in page
    missing = _run("skill_findings", {"skill": "nope"}, skills_payload)
    assert "error" in missing and "scan_boundary" in missing["hint"]


def test_read_guidance_lists_sections_then_reads_one(skills_payload):
    docs = _run("read_guidance", {}, skills_payload)["documents"]
    ids = {d["skill_id"] for d in docs}
    assert {"guide", "dft_atpg_debug"} <= ids
    toc = _run("read_guidance", {"skill": "guide"}, skills_payload)
    assert toc["sections"] == ["Ties"]
    sec = _run("read_guidance", {"skill": "guide", "section": "tie"},
               skills_payload)
    assert "TIEHI" in sec["text"] and sec["note"] == GUIDANCE_NOTE


def test_bulk_results_mark_shortened_lists():
    result = SkillResult(skill_id="x")
    result.add_finding(title="t", description="d",
                       affected_objects=[f"o{i}" for i in range(25)],
                       evidence=[f"e{i}" for i in range(3)])
    data = json.loads(_serialize_skill_result(result))
    entry = data["findings"][0]
    assert len(entry["affected_objects"]) == 10
    assert entry["affected_total"] == 25
    assert "evidence_total" not in entry


def test_http_loop_offers_query_tools_and_returns_their_json(demo_report):
    seen = {}

    class _Agent(DebugAgent):
        def __init__(self):
            super().__init__(AgentConfig(backend="http", base_url="http://x",
                                         model="m"))
            self.rounds = 0

        def _post_chat(self, messages, tools=None):
            self.rounds += 1
            if self.rounds == 1:
                seen["tools"] = {t["function"]["name"] for t in tools}
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "1", "function": {
                        "name": "report_context", "arguments": "{}"}}]}
            seen["result"] = messages[-1]["content"]
            return {"role": "assistant", "content": "done"}

    manager = SkillManager()
    ctx = AnalysisContext(
        netlist=demo_report.netlist, faults=demo_report.faults,
        constraints=demo_report.constraints,
        fault_results=demo_report.fault_results,
        pattern_groups=demo_report.pattern_groups, summary=demo_report.summary,
        context=investigate.serialize_context(demo_report))
    _Agent()._tool_loop([{"role": "user", "content": "x"}], manager, ctx,
                        lambda m: None)
    assert seen["tools"] == set(investigate.TOOL_SPECS)
    assert "census" in json.loads(seen["result"])


def test_mcp_server_answers_skill_findings_from_the_evidence(
        tmp_path, skills_payload, demo_report):
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, None,
        skills=skills_payload)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    state = mcp_server.load_state(str(path))
    resp = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "read_guidance", "arguments": {}}}, state)
    data = json.loads(resp["result"]["content"][0]["text"])
    assert any(d["skill_id"] == "guide" for d in data["documents"])


def test_old_regression_names_still_answer_over_mcp(tmp_path, demo_report):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(investigate.export_evidence(
        demo_report.fault_results, [], None)))
    state = mcp_server.load_state(str(path))
    resp = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "list_regressed", "arguments": {}}}, state)
    data = json.loads(resp["result"]["content"][0]["text"])
    assert "Compare Report" in data["hint"]


# ---------------------------------------------------------------------------
# Step 3: paging, detail level, reading a spill
# ---------------------------------------------------------------------------
def test_list_faults_pages_filters_by_issue_and_stays_compact(demo_report):
    frs = demo_report.fault_results
    first = investigate.list_faults(frs, limit=5)
    assert first["returned"] == 5 and first["more"] is True
    assert "evidence" not in first["faults"][0]
    second = investigate.list_faults(frs, limit=5,
                                     offset=first["next_offset"])
    assert second["faults"][0] != first["faults"][0]
    obs = investigate.list_faults(frs, issue="observability", limit=1000)
    assert all(fr.observability_issue for fr in frs
               if fr.fault.fault_object in
               {f["fault_object"] for f in obs["faults"]})
    assert "hint" in investigate.list_faults(frs, issue="bogus")
    full = investigate.list_faults(frs, limit=1, detail="full")
    assert "evidence" in full["faults"][0]


def test_get_fault_detail_pages_with_limit(demo_report):
    frs = demo_report.fault_results
    out = investigate.run_tool("get_fault_detail",
                               {"fault": "/", "limit": 2, "detail": "summary"},
                               fault_results=frs, constraints=[], netlist=None)
    assert out["returned"] == 2 and out["more"] is True
    assert "evidence" not in out["faults"][0]


def test_read_spill_pages_and_refuses_paths_outside_the_spill_dir(tmp_path):
    spill = tmp_path / "spill"
    spill.mkdir()
    big = spill / "list_faults_1.json"
    big.write_text("x" * 50000)
    first = _run("read_spill", {"path": str(big)}, spill_dir=str(spill))
    assert first["total_chars"] == 50000 and first["more"] is True
    rest = _run("read_spill", {"path": str(big),
                               "offset": first["next_offset"]},
                spill_dir=str(spill))
    assert first["returned_chars"] + rest["returned_chars"] == 50000
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    assert "error" in _run("read_spill", {"path": str(outside)},
                           spill_dir=str(spill))
    assert "error" in _run("read_spill",
                           {"path": str(spill / ".." / "secret.txt")},
                           spill_dir=str(spill))
    assert "hint" in _run("read_spill", {"path": str(big)})


def test_list_params_are_arrays_in_both_schemas():
    tool = next(t for t in mcp_server.build_tools_list()
                if t["name"] == "verify_paths")
    assert tool["inputSchema"]["properties"]["paths"]["type"] == "array"
    skill = SkillManager().get("verify_paths")
    prop = skill.to_tool_schema()["function"]["parameters"]["properties"]
    assert prop["paths"]["type"] == "array"


def test_unknown_tool_error_names_the_alternatives():
    out = _run("no_such_tool", {})
    assert "error" in out and "report_context" in out["hint"]


def test_skills_tab_says_how_each_skill_reaches_the_agent():
    pytest.importorskip("PySide6")
    from atpg_coverage_debug_agent.gui.skills_panel import agent_reach
    manager = SkillManager()
    assert "skill_findings" in agent_reach(manager.get("scan_boundary"))
    assert "Deep investigation" in agent_reach(manager.get("list_faults"))
