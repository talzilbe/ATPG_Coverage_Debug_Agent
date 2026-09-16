"""Tests for the offline <-> agent hand-off loop.

Covers the netlist hand-off to the out-of-process tools, the persistent MCP
session that keeps follow-up turns agentic, the structured findings channel,
the classification cross-check, the open-questions list and the tool-call log
-- i.e. everything that turns the hand-off from one-way into a loop.
"""

from __future__ import annotations

import json
import os

import pytest

from atpg_coverage_debug_agent import mcp_server, session
from atpg_coverage_debug_agent.agent import debug_agent
from atpg_coverage_debug_agent.agent.debug_agent import (
    AGENTIC_SYSTEM_PROMPT,
    AgentConfig,
    DebugAgent,
    McpSession,
    build_user_payload,
)
from atpg_coverage_debug_agent.analysis import agreement, findings, investigate
from atpg_coverage_debug_agent.analysis import open_questions as oq
from atpg_coverage_debug_agent.analysis.report_edit import apply_exclusions
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.models import RootCause
from atpg_coverage_debug_agent.parser import netlist_cache
from atpg_coverage_debug_agent.reporting.html_report import build_html_report
from atpg_coverage_debug_agent.reporting.markdown_report import render_markdown
from atpg_coverage_debug_agent.reporting.session_report import (
    dict_to_report,
    report_to_dict,
)
from atpg_coverage_debug_agent.skills.base import AnalysisContext
from atpg_coverage_debug_agent.skills.manager import SkillManager

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_HERE, "sample_data")
_NETLIST = os.path.join(_SAMPLE, "demo_netlist.v")
_FAULTS = os.path.join(_SAMPLE, "demo_faults.mtfi")
_CONSTRAINTS = os.path.join(_SAMPLE, "demo_constraints.do")


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(_NETLIST, _FAULTS, _CONSTRAINTS)


def _ctx(report, sink=None):
    return AnalysisContext(
        netlist=report.netlist, faults=report.faults,
        constraints=report.constraints, fault_results=report.fault_results,
        pattern_groups=report.pattern_groups, summary=report.summary,
        triage=investigate.serialize_triage(
            report.statistics, report.selected_categories,
            report.recommendations),
        context=investigate.serialize_context(report),
        design=investigate.serialize_design(report.netlist, report),
        findings=sink)


# ---------------------------------------------------------------------------
# A. Netlist hand-off
# ---------------------------------------------------------------------------
def test_netlist_cache_round_trips_and_is_keyed_on_the_source(tmp_path):
    src = tmp_path / "n.v"
    src.write_text("module top(input a, output y); buf u0 (.A(a), .Y(y)); "
                   "endmodule\n")
    first, origin1 = netlist_cache.load_or_parse(str(src), use_cache=True)
    second, origin2 = netlist_cache.load_or_parse(str(src), use_cache=True)
    assert origin1 in ("parsed", "parsed_uncached")
    assert origin2 == "cache"
    assert set(second.modules) == set(first.modules)

    # Touching the file changes the key: the stale pickle is not reused.
    src.write_text(src.read_text() + "\n// changed\n")
    os.utime(str(src), None)
    _, origin3 = netlist_cache.load_or_parse(str(src), use_cache=True)
    assert origin3 != "cache"


def test_a_corrupt_pickle_is_ignored_not_fatal(tmp_path):
    bad = tmp_path / "bad.pkl"
    bad.write_bytes(b"not a pickle")
    assert netlist_cache.load(str(bad)) is None


def test_the_analysis_records_where_its_netlist_came_from(demo_report):
    assert demo_report.sources.get("netlist_origin") in (
        "cache", "parsed", "parsed_uncached")


def test_handoff_writes_a_pickle_the_server_can_load(demo_report, tmp_path):
    path = netlist_cache.handoff_path(demo_report.netlist, None, str(tmp_path))
    assert path and os.path.isfile(path)
    loaded = netlist_cache.load(path)
    assert set(loaded.modules) == set(demo_report.netlist.modules)


def test_mcp_server_loads_the_netlist_lazily_for_netlist_tools(demo_report,
                                                               tmp_path,
                                                               monkeypatch):
    pkl = netlist_cache.handoff_path(demo_report.netlist, None, str(tmp_path))
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints,
        demo_report.netlist,
        design=investigate.serialize_design(demo_report.netlist, demo_report))
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence))
    monkeypatch.setenv(netlist_cache.NETLIST_FILE_ENV, pkl)
    monkeypatch.setenv(session.SESSION_DIR_ENV, str(tmp_path))
    state = mcp_server.load_state(str(ev_path))
    assert state["netlist"] is None, "must not load before it is needed"

    # A census question does not pay for a design load.
    mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "coverage_triage", "arguments": {}}}, state)
    assert state["netlist"] is None

    fo = demo_report.fault_results[0].fault.fault_object
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "scan_status", "arguments": {"target": fo}}}, state)
    assert state["netlist"] is not None
    assert state["netlist_origin"] == "handoff_pickle"
    data = json.loads(resp["result"]["content"][0]["text"])
    assert data["netlist_origin"] == "handoff_pickle"
    assert state["design"]["netlist_live"] is True


def test_mcp_server_falls_back_to_reparsing_the_source(demo_report, tmp_path,
                                                       monkeypatch):
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints,
        demo_report.netlist,
        design=investigate.serialize_design(demo_report.netlist, demo_report))
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence))
    monkeypatch.delenv(netlist_cache.NETLIST_FILE_ENV, raising=False)
    state = mcp_server.load_state(str(ev_path))
    netlist = mcp_server.ensure_netlist(state)
    assert netlist is not None
    assert state["netlist_origin"] in ("cache", "parsed", "parsed_uncached")


def test_trace_path_prefers_the_live_netlist_over_the_adjacency(demo_report):
    frm = demo_report.fault_results[0].mapping.instance_name
    with_netlist = investigate.run_tool(
        "trace_path", {"from_instance": frm, "to_instance": "nowhere"},
        fault_results=demo_report.fault_results, constraints=[],
        netlist=demo_report.netlist, adjacency={})
    # An empty adjacency alone would have found nothing and said so.
    assert "serialised adjacency" not in (with_netlist.get("note") or "")


# ---------------------------------------------------------------------------
# B. Persistent MCP session + agentic follow-ups
# ---------------------------------------------------------------------------
class _RecordingCli(DebugAgent):
    def __init__(self, **cfg):
        super().__init__(AgentConfig(backend="cli", cli_path=__file__, **cfg))
        self.calls = []

    def _call_cli(self, system_prompt, user_payload, session_id=None,
                  resume=False, extra_args=None, on_chunk=None):
        self.calls.append({"system": system_prompt, "payload": user_payload,
                           "session_id": session_id, "resume": resume,
                           "extra_args": list(extra_args or [])})
        return "answer without any path or percentage claim"


def test_the_mcp_session_survives_the_first_answer(demo_report):
    agent = _RecordingCli()
    ctx = _ctx(demo_report)
    try:
        agent._run_agentic_cli_mcp(demo_report, ctx, lambda m: None,
                                   session_id="sid")
        sess = agent.mcp_session
        assert isinstance(sess, McpSession)
        assert sess.alive, "the server config must still exist afterwards"
        assert os.path.isfile(sess.evidence_path)
        assert sess.netlist_path and os.path.isfile(sess.netlist_path)
        cfg = json.load(open(sess.config_path))
        env = cfg["mcpServers"]["atpg"]["env"]
        assert env[netlist_cache.NETLIST_FILE_ENV] == sess.netlist_path
        assert env[session.SESSION_DIR_ENV] == sess.work_dir
        assert "--additional-mcp-config" in agent.calls[0]["extra_args"]
        assert "list_open_questions" in agent.calls[0]["payload"]
        assert "record_finding" in agent.calls[0]["payload"]
    finally:
        if agent.mcp_session:
            agent.mcp_session.close()
    assert not os.path.isdir(sess.work_dir), "close() removes the session"


def test_a_follow_up_turn_reattaches_the_tool_server(demo_report):
    agent = _RecordingCli()
    ctx = _ctx(demo_report)
    try:
        agent._run_agentic_cli_mcp(demo_report, ctx, lambda m: None,
                                   session_id="sid")
        events = []
        agent.chat("why?", session_id="sid", report=demo_report,
                   on_event=events.append)
        follow = agent.calls[-1]
        assert follow["resume"] is True
        assert follow["extra_args"] == agent.mcp_session.extra_args()
        assert "--additional-mcp-config" in follow["extra_args"]
        assert any("tools attached" in e for e in events)
    finally:
        if agent.mcp_session:
            agent.mcp_session.close()


def test_a_follow_up_without_a_session_says_it_has_no_tools(demo_report):
    agent = _RecordingCli()
    events = []
    agent.chat("why?", session_id="sid", on_event=events.append)
    assert agent.calls[-1]["extra_args"] == []
    assert any("WITHOUT tools" in e for e in events)


def test_a_new_run_closes_the_previous_session(demo_report):
    agent = _RecordingCli()
    ctx = _ctx(demo_report)
    agent._run_agentic_cli_mcp(demo_report, ctx, lambda m: None)
    first = agent.mcp_session.work_dir
    try:
        agent._run_agentic_cli_mcp(demo_report, ctx, lambda m: None)
        assert not os.path.isdir(first)
        assert agent.mcp_session.work_dir != first
    finally:
        agent.mcp_session.close()


def test_a_follow_up_answer_gets_the_guardrail_correction(demo_report):
    class _Bad(_RecordingCli):
        def _call_cli(self, *a, **k):
            super()._call_cli(*a, **k)
            return "This fix will recover 12% coverage."

        def run_with_prompt(self, system_prompt, user_payload):
            self.calls.append({"correction": True})
            return "This fix may help; only a re-run can measure it."

    agent = _Bad()
    reply = agent.chat("how much?", session_id="sid", report=demo_report)
    assert "12%" not in reply
    assert any(c.get("correction") for c in agent.calls)


def test_an_http_follow_up_runs_through_the_tool_loop(demo_report):
    """The HTTP chat must be able to call tools, and the exchange must land
    in the conversation history the panel keeps."""
    sink = findings.FindingsSink()
    ctx = _ctx(demo_report, sink)
    manager = SkillManager()
    fo = demo_report.fault_results[0].fault.fault_object

    class _Scripted(DebugAgent):
        def __init__(self):
            super().__init__(AgentConfig(backend="http", base_url="http://x",
                                         model="m"))
            self.rounds = 0

        def _post_chat(self, messages, tools=None):
            self.rounds += 1
            if self.rounds == 1:
                assert tools, "the follow-up must be offered the tools"
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "record_finding",
                        "arguments": json.dumps({
                            "kind": "confirmation", "subject": fo,
                            "field": "root_cause",
                            "offline_value": "x", "evidence": "why_blocked"})}}]}
            return {"role": "assistant", "content": "done, no claims"}

    agent = _Scripted()
    history = [{"role": "system", "content": AGENTIC_SYSTEM_PROMPT},
               {"role": "user", "content": "payload"},
               {"role": "assistant", "content": "first answer"},
               {"role": "user", "content": "confirm the first fault"}]
    reply = agent.chat("confirm", history=history, report=demo_report,
                       skill_manager=manager, ctx=ctx)
    assert reply == "done, no claims"
    roles = [m["role"] for m in history]
    assert roles[-2:] == ["assistant", "tool"], "tool exchange kept in history"
    assert len(sink.items) == 1 and sink.items[0].subject == fo


# ---------------------------------------------------------------------------
# C. Findings back-channel
# ---------------------------------------------------------------------------
def test_record_finding_validates_and_verifies_the_subject(demo_report):
    sink = findings.FindingsSink()
    fo = demo_report.fault_results[0].fault.fault_object
    ok = investigate.run_tool(
        "record_finding",
        {"kind": "correction", "subject": fo, "field": "root_cause",
         "offline_value": "a", "agent_value": "b", "evidence": "tool X"},
        fault_results=demo_report.fault_results, constraints=[], netlist=None,
        findings=sink)
    assert ok["recorded"] and ok["finding"]["subject_verified"] is True
    assert "warning" not in ok

    cat = investigate.run_tool(
        "record_finding",
        {"kind": "gap", "subject": "au.tc", "field": "category_ranking"},
        fault_results=demo_report.fault_results, constraints=[], netlist=None,
        triage=investigate.serialize_triage(
            demo_report.statistics, demo_report.selected_categories,
            demo_report.recommendations),
        findings=sink)
    assert cat["finding"]["subject_verified"] is True

    unknown = investigate.run_tool(
        "record_finding",
        {"kind": "new_lead", "subject": "/top/made/up", "evidence": "e"},
        fault_results=demo_report.fault_results, constraints=[], netlist=None,
        findings=sink)
    assert unknown["recorded"] and unknown["finding"]["subject_verified"] is False
    assert "warning" in unknown

    for bad in ({"kind": "opinion", "subject": fo},
                {"kind": "correction", "subject": ""},
                {"kind": "correction", "subject": fo, "agent_value": ""},
                {"kind": "correction", "subject": fo, "agent_value": "v",
                 "evidence": ""},
                {"kind": "gap", "subject": fo, "field": "colour"},
                {"kind": "gap", "subject": fo, "confidence": "certain"}):
        out = investigate.run_tool("record_finding", bad,
                                   fault_results=demo_report.fault_results,
                                   constraints=[], netlist=None, findings=sink)
        assert "error" in out, bad
    assert len(sink.items) == 3


def test_record_finding_without_a_sink_refuses(demo_report):
    out = investigate.run_tool(
        "record_finding", {"kind": "gap", "subject": "x"},
        fault_results=demo_report.fault_results, constraints=[], netlist=None)
    assert "error" in out


def test_findings_persist_to_jsonl_and_read_back(tmp_path):
    path = str(tmp_path / findings.FINDINGS_FILE)
    sink = findings.FindingsSink(path)
    sink.record(findings.Finding(kind="gap", subject="AU.TC", field="other"))
    sink.record(findings.Finding(kind="new_lead", subject="/a/b",
                                 evidence="e"))
    back = findings.read_findings(path)
    assert [f.kind for f in back] == ["gap", "new_lead"]
    assert findings.read_findings(str(tmp_path / "missing.jsonl")) == []
    summary = findings.summarize(back)
    assert summary["total"] == 2 and summary["by_kind"]["gap"] == 1


def test_the_mcp_server_persists_findings_into_the_session_dir(demo_report,
                                                               tmp_path,
                                                               monkeypatch):
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, None,
        adjacency={})
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence))
    monkeypatch.setenv(session.SESSION_DIR_ENV, str(tmp_path))
    state = mcp_server.load_state(str(ev_path))
    fo = demo_report.fault_results[0].fault.fault_object
    mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "record_finding", "arguments": {
            "kind": "confirmation", "subject": fo, "field": "scan_status"}}},
        state)
    back = findings.read_findings(str(tmp_path / findings.FINDINGS_FILE))
    assert len(back) == 1 and back[0].subject == fo
    sess = McpSession(work_dir=str(tmp_path), config_path="", evidence_path="")
    assert [f.subject for f in sess.findings()] == [fo]


def test_findings_reach_the_reports_beside_the_offline_values(demo_report):
    fo = demo_report.fault_results[0].fault.fault_object
    demo_report.investigation = {"diagnosis": "d", "chat": [], "trace": "",
                                 "findings": [{
                                     "kind": "correction", "subject": fo,
                                     "field": "root_cause",
                                     "offline_value": "other_structural_cause",
                                     "agent_value": "tied_or_constant_hardware",
                                     "evidence": "why_blocked",
                                     "confidence": "high",
                                     "subject_verified": True}]}
    try:
        html = build_html_report(demo_report)
        md = render_markdown(demo_report)
    finally:
        demo_report.investigation = None
    assert "9.2 Agent review" in html
    assert "tied_or_constant_hardware" in html and "other_structural_cause" in html
    assert "## Agent Review" in md and fo in md
    # Without findings the sub-section is absent, not empty.
    assert "9.2 Agent review" not in build_html_report(demo_report)
    assert "## Agent Review" not in render_markdown(demo_report)


# ---------------------------------------------------------------------------
# E. Classification cross-check
# ---------------------------------------------------------------------------
def test_cross_check_verdicts():
    tied = RootCause.TIED_CONSTANT.value
    other = RootCause.OTHER_STRUCTURAL.value
    scan = RootCause.SCAN_TO_NON_SCAN.value
    unres = RootCause.UNRESOLVED_CONNECTIVITY.value
    assert agreement.judge("AU.TC", tied, True) == agreement.AGREE
    assert agreement.judge("AU.TC", scan, True) == agreement.DISAGREE
    assert agreement.judge("AU.TC", other, True) == agreement.UNCONFIRMED
    assert agreement.judge("UO.AAB", tied, True) == agreement.DISAGREE
    assert agreement.judge("UO.AAB", other, True) == agreement.UNINFORMATIVE
    assert agreement.judge("AU.TC", tied, False) == agreement.NOT_MEASURED
    assert agreement.judge("AU.TC", unres, True) == agreement.NOT_MEASURED
    assert agreement.judge("AU.XYZ", tied, True) == agreement.UNINFORMATIVE


def test_cross_check_over_the_demo_sums_to_the_loss(demo_report):
    agr = demo_report.agreement
    assert agr is not None
    assert sum(agr.totals.values()) == len(demo_report.fault_results)
    assert sum(p.count for p in agr.pairs) == len(demo_report.fault_results)
    # The demo's TDR-driven AU.TC faults are not resolved per fault, which is
    # exactly the hole this check exists to surface.
    assert agr.totals[agreement.UNCONFIRMED] > 0
    lead_subs = {x["subclass"] for x in agr.leads}
    assert "AU.TC" in lead_subs
    for lead in agr.leads:
        assert lead["samples"], "a lead must carry verbatim samples"
        assert lead["why_it_matters"]


def test_cross_check_survives_the_session_round_trip(demo_report):
    data = json.loads(json.dumps(report_to_dict(demo_report), default=str))
    back = dict_to_report(data)
    assert back.agreement.totals == demo_report.agreement.totals
    assert [q.id for q in back.open_questions] == \
        [q.id for q in demo_report.open_questions]
    # An older session without the keys rebuilds what it can.
    data.pop("agreement")
    data.pop("open_questions")
    older = dict_to_report(data)
    assert older.agreement is not None
    assert older.agreement.totals == demo_report.agreement.totals
    assert isinstance(older.open_questions, list)


def test_cross_check_is_recomputed_after_waivers(demo_report):
    edited = apply_exclusions(demo_report, excluded_subtypes=["AU.TC"])
    assert "AU.TC" not in edited.agreement.by_subclass
    assert all(q.subject != "AU.TC" for q in edited.open_questions)


def test_cross_check_tool_filters_and_reaches_the_context(demo_report):
    ctx = investigate.serialize_context(demo_report)
    assert "classification_crosscheck" in ctx and "open_questions" in ctx
    out = investigate.run_tool(
        "classification_crosscheck",
        {"subclass": "AU.TC", "only_disagreements": True},
        fault_results=demo_report.fault_results, constraints=[], netlist=None,
        context=ctx)
    assert "pairs" not in out
    assert set(out["by_subclass"]) == {"AU.TC"}
    assert all(x["subclass"] == "AU.TC" for x in out["leads"])
    via_context = investigate.run_tool(
        "report_context", {"section": "classification_crosscheck"},
        fault_results=[], constraints=[], netlist=None, context=ctx)
    assert "classification_crosscheck" in via_context


# ---------------------------------------------------------------------------
# D. Open questions
# ---------------------------------------------------------------------------
def test_open_questions_are_ordered_and_name_a_tool(demo_report):
    qs = demo_report.open_questions
    assert qs, "the demo has recorded weak spots"
    assert [q.priority for q in qs] == sorted(q.priority for q in qs)
    for q in qs:
        assert q.suggested_tools, q.id
        for tool in q.suggested_tools:
            assert tool in investigate.TOOL_SPECS, tool
        assert q.question and q.why


def test_open_questions_surface_the_recorded_weak_spots(demo_report):
    ids = {q.id for q in demo_report.open_questions}
    # The reconvergent demo block is structurally mixed.
    assert "category:UO.AAB:mixed_profile" in ids
    # Every unconfirmed cross-check lead becomes a question.
    assert any(i.startswith("agreement:AU.TC:") for i in ids)


def test_open_questions_react_to_population_level_problems(demo_report):
    class _Stats:  # a summary with most of the loss unmapped
        coverage_loss_count = 100
        unmapped_count = 60

    class _Report:
        summary = _Stats()
        disposition = None
        constraint_diagnostics = {"unresolved": 3, "directives": 10}
        selected_categories = []
        agreement = None
        statistics = None
        fault_results = []

    qs = oq.build_open_questions(_Report())
    ids = {q.id for q in qs}
    assert "mapping:unmapped_share" in ids
    assert "constraints:partially_parsed" in ids
    assert qs[0].id == "mapping:unmapped_share", "unmapped share is priority 1"


def test_open_questions_tool_filters_by_subject(demo_report):
    ctx = investigate.serialize_context(demo_report)
    out = investigate.run_tool("list_open_questions", {"subject": "uo.aab"},
                               fault_results=[], constraints=[], netlist=None,
                               context=ctx)
    assert out["total"] >= 1
    assert all(q["subject"] == "UO.AAB" for q in out["questions"])
    none = investigate.run_tool("list_open_questions", {"subject": "nothing"},
                                fault_results=[], constraints=[], netlist=None,
                                context=ctx)
    assert none["total"] == 0 and none["questions"] == []


def test_open_questions_reach_every_surface(demo_report):
    payload = build_user_payload(demo_report, agentic=True)
    assert "## Open Questions" in payload
    assert "settle with:" in payload
    assert "Subclass vs structural root cause" in payload
    md = render_markdown(demo_report)
    assert "## Open Questions" in md and "## Classification Cross-check" in md
    html = build_html_report(demo_report)
    assert "4.6 Does the structural root cause agree" in html
    assert "4.7 Open questions" in html


def test_the_agentic_prompt_points_at_the_new_tools():
    for name in ("list_open_questions", "classification_crosscheck",
                 "record_finding"):
        assert name in AGENTIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# F. Tool-call log
# ---------------------------------------------------------------------------
def test_every_mcp_tool_call_is_logged_in_the_session_dir(demo_report,
                                                           tmp_path,
                                                           monkeypatch):
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, None,
        adjacency={})
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence))
    monkeypatch.setenv(session.SESSION_DIR_ENV, str(tmp_path))
    state = mcp_server.load_state(str(ev_path))
    mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_faults", "arguments": {"limit": 1}}}, state)
    mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_fault_detail", "arguments": {"fault": "zz"}}},
        state)
    rows = [json.loads(l) for l in
            (tmp_path / mcp_server.TOOL_LOG_FILE).read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["list_faults", "get_fault_detail"]
    assert rows[0]["args"] == {"limit": 1}
    assert all(r["ok"] for r in rows) and all("ms" in r for r in rows)
    sess = McpSession(work_dir=str(tmp_path), config_path="", evidence_path="")
    assert sess.tool_log_path == str(tmp_path / debug_agent.TOOL_LOG_FILE)


def test_no_session_dir_means_no_log_and_no_error(demo_report, tmp_path,
                                                  monkeypatch):
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, None,
        adjacency={})
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence))
    monkeypatch.delenv(session.SESSION_DIR_ENV, raising=False)
    state = mcp_server.load_state(str(ev_path))
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_faults", "arguments": {"limit": 1}}}, state)
    assert "result" in resp
    assert not os.path.exists(tmp_path / mcp_server.TOOL_LOG_FILE)


# ---------------------------------------------------------------------------
# G. One tool surface
# ---------------------------------------------------------------------------
def test_every_mcp_tool_has_a_skill_and_vice_versa():
    """The HTTP loop exposes skills, the MCP server exposes TOOL_SPECS. They
    must be the same set, or the two backends investigate with different
    tools without anyone noticing."""
    manager = SkillManager()
    on_demand = {s.skill_id for s in manager.skills
                 if getattr(s, "on_demand", False)}
    assert on_demand == set(investigate.TOOL_SPECS)
    for skill in manager.skills:
        if getattr(skill, "on_demand", False):
            assert skill.tool_name == skill.skill_id
