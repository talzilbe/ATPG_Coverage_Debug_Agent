"""Tests for the agent's fix-plan contribution and for stopping a turn.

The fix plan is an overlay: the offline plan is never mutated, the agent's
edits are records applied on read, and every renderer shows the same result.
Stopping is cooperative: the token is checked at every boundary a turn
already has, the live subprocess is terminated, and the partial output is
kept while the conversation survives.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time

import pytest

from atpg_coverage_debug_agent import mcp_server, session
from atpg_coverage_debug_agent.agent.debug_agent import (
    AGENTIC_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    AgentCancelled,
    AgentConfig,
    CancelToken,
    DebugAgent,
    McpSession,
)
from atpg_coverage_debug_agent.analysis import fix_plan_edits as fpe
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.app import run_analysis
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


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(os.path.join(_SAMPLE, "demo_netlist.v"),
                        os.path.join(_SAMPLE, "demo_faults.mtfi"),
                        os.path.join(_SAMPLE, "demo_constraints.do"))


@pytest.fixture
def plan_kwargs(demo_report):
    sink = fpe.FixEditsSink()
    triage = investigate.serialize_triage(
        demo_report.statistics, demo_report.selected_categories,
        demo_report.recommendations)
    return sink, dict(fault_results=demo_report.fault_results,
                      constraints=demo_report.constraints, netlist=None,
                      triage=triage, fix_edits=sink)


def _propose(kw, **args):
    return investigate.run_tool("propose_fix", args, **kw)


# ---------------------------------------------------------------------------
# Overlay semantics
# ---------------------------------------------------------------------------
def test_amend_attaches_a_note_and_leaves_the_offline_text_alone(
        demo_report, plan_kwargs):
    sink, kw = plan_kwargs
    base = demo_report.recommendations
    out = _propose(kw, action="amend", target_rank=1,
                   note="Run the what-if before the RTL change.")
    assert out["recorded"] and out["edits_so_far"] == 1
    plan = fpe.apply_fix_plan_edits(base, sink.items)
    assert plan[0].rank == 1 and plan[0].origin == "offline"
    assert plan[0].agent_notes == ["[agent] Run the what-if before the RTL "
                                   "change."]
    assert plan[0].fix.rationale == base[0].fix.rationale
    assert base[0].agent_notes == [], "the offline plan is never mutated"
    assert len(plan) == len(base)


def test_add_appends_an_agent_proposal_that_must_be_measured(
        demo_report, plan_kwargs):
    sink, kw = plan_kwargs
    out = _propose(kw, action="add", subclass="au.tc",
                   title="Reprogram the TDR", rationale="It is programmable.",
                   commands="set_test_data_register a\nset_test_data_register b",
                   preconditions="Confirm the TDR is in the ICL",
                   effort="low", risk="low", evidence="list_blocking_sources",
                   confidence="high")
    assert out["recorded"]
    plan = fpe.apply_fix_plan_edits(demo_report.recommendations, sink.items)
    new = plan[-1]
    assert new.origin == "agent" and new.rank == len(plan)
    assert new.subclass_id == "AU.TC", "subclass id normalised to the census"
    assert new.title.startswith("[agent]")
    assert new.fix.commands == ["set_test_data_register a",
                                "set_test_data_register b"]
    assert new.fix.preconditions == ["Confirm the TDR is in the ICL"]
    assert new.fix.requires_measurement is True
    assert new.evidence == ["[agent] list_blocking_sources"]
    assert new.fault_count == next(r for r in demo_report.recommendations
                                   if r.subclass_id == "AU.TC").fault_count


def test_replace_takes_the_slot_and_keeps_the_offline_entry_visible(
        demo_report, plan_kwargs):
    sink, kw = plan_kwargs
    base = demo_report.recommendations
    target = base[1]
    out = _propose(kw, action="replace", target_rank=2, title="Better fix",
                   rationale="A cheaper route.", reason="cheaper and safer",
                   evidence="why_blocked", commands=["cmd"])
    assert out["recorded"]
    plan = fpe.apply_fix_plan_edits(base, sink.items)
    assert [r.rank for r in plan] == list(range(1, len(plan) + 1))
    winner = plan[1]
    assert winner.origin == "agent" and winner.supersedes == 2
    assert winner.edit_reason == "cheaper and safer"
    demoted = plan[-1]
    assert demoted.fix.fix_id == target.fix.fix_id
    assert demoted.superseded and demoted.superseded_by == winner.rank
    assert any("superseded by proposal #2" in n for n in demoted.agent_notes)
    assert demoted.fix.rationale == target.fix.rationale
    # The tool's own view of the plan matches.
    assert out["plan"][1]["origin"] == "agent"
    assert out["plan"][-1]["superseded_by"] == 2


def test_replacing_the_same_slot_twice_supersedes_the_agents_own_proposal(
        demo_report, plan_kwargs):
    """The agent may change its mind: the newer proposal takes the slot and
    the earlier one is demoted like any superseded entry, so nothing the
    agent said disappears either."""
    sink, kw = plan_kwargs
    _propose(kw, action="replace", target_rank=1, title="A", rationale="r",
             reason="x", evidence="e")
    _propose(kw, action="replace", target_rank=1, title="B", rationale="r",
             reason="y", evidence="e")
    plan = fpe.apply_fix_plan_edits(demo_report.recommendations, sink.items)
    assert plan[0].origin == "agent" and plan[0].title.endswith("B")
    demoted = [r for r in plan if r.superseded]
    assert len(demoted) == 2, "the offline entry AND proposal A are demoted"
    assert any(r.origin == "agent" and r.title.endswith("A") for r in demoted)
    assert any(r.origin == "offline" for r in demoted)
    assert all(r.superseded_by == 1 for r in demoted)
    assert len([r for r in plan if r.origin == "agent"]) == 2


# ---------------------------------------------------------------------------
# Validation: the agent is held to the offline plan's rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("args", [
    {"action": "delete", "subclass": "AU.TC"},
    {"action": "amend", "note": "x"},                      # no target
    {"action": "amend", "target_rank": 999, "note": "x"},  # unknown rank
    {"action": "amend", "target_rank": 1},                 # no note
    {"action": "add", "subclass": "ZZ.YY", "title": "t", "rationale": "r",
     "evidence": "e"},                                     # unknown category
    {"action": "add", "subclass": "AU.TC", "title": "t"},  # missing fields
    {"action": "replace", "target_rank": 1, "title": "t", "rationale": "r",
     "evidence": "e"},                                     # no reason
    {"action": "add", "subclass": "AU.TC", "title": "t", "rationale": "r",
     "evidence": "e", "effort": "huge"},
    {"action": "add", "subclass": "AU.TC", "title": "t", "rationale": "r",
     "evidence": "e", "confidence": "certain"},
])
def test_invalid_proposals_are_refused(plan_kwargs, args):
    sink, kw = plan_kwargs
    out = _propose(kw, **args)
    assert "error" in out, args
    assert sink.items == []


def test_a_predicted_gain_or_an_invented_path_is_refused(plan_kwargs):
    sink, kw = plan_kwargs
    gain = _propose(kw, action="add", subclass="AU.TC", title="t",
                    rationale="This will recover 12% coverage.", evidence="e")
    assert "error" in gain and any("coverage gain" in i for i in gain["issues"])
    path = _propose(kw, action="amend", target_rank=1,
                    note="Check /top/u_invented/never_there_reg/Q first.")
    assert "error" in path and path["issues"]
    real = _propose(kw, action="amend", target_rank=1,
                    note=f"Check {kw['fault_results'][0].fault.fault_object}.")
    assert real.get("recorded"), "a path from the inputs is fine"
    assert len(sink.items) == 1


def test_without_a_sink_or_a_plan_the_tool_refuses(demo_report, plan_kwargs):
    _, kw = plan_kwargs
    kw = dict(kw, fix_edits=None)
    assert "error" in _propose(kw, action="amend", target_rank=1, note="n")
    kw = dict(kw, triage=None)
    assert "error" in _propose(kw, action="amend", target_rank=1, note="n")


# ---------------------------------------------------------------------------
# Every reader sees the same overlaid plan
# ---------------------------------------------------------------------------
def test_recommend_fixes_returns_the_overlaid_plan(demo_report, plan_kwargs):
    sink, kw = plan_kwargs
    _propose(kw, action="add", subclass="AU.TC", title="T", rationale="r",
             evidence="e")
    out = investigate.run_tool("recommend_fixes", {"limit": 50}, **kw)
    assert out["agent_edits_applied"] == 1
    assert out["recommendations"][-1]["origin"] == "agent"
    only = investigate.run_tool("recommend_fixes",
                                {"subclass": "AU.TC", "limit": 50}, **kw)
    assert any(r["origin"] == "agent" for r in only["recommendations"])
    clean = investigate.run_tool("recommend_fixes", {},
                                 **dict(kw, fix_edits=None))
    assert "agent_edits_applied" not in clean


def test_effective_plan_reads_the_investigation_and_survives_save_load(
        demo_report, plan_kwargs):
    sink, kw = plan_kwargs
    _propose(kw, action="amend", target_rank=1, note="note one")
    _propose(kw, action="add", subclass="AU.TC", title="T", rationale="r",
             evidence="e")
    demo_report.investigation = {"fix_plan_edits": sink.as_dicts()}
    try:
        plan = fpe.effective_plan(demo_report)
        assert plan[0].agent_notes == ["[agent] note one"]
        assert plan[-1].origin == "agent"
        assert demo_report.recommendations[0].agent_notes == []
        data = json.loads(json.dumps(report_to_dict(demo_report), default=str))
        back = dict_to_report(data)
        again = fpe.effective_plan(back)
        assert [(r.rank, r.origin, r.agent_notes) for r in again] == \
            [(r.rank, r.origin, r.agent_notes) for r in plan]
    finally:
        demo_report.investigation = None
    assert fpe.effective_plan(demo_report) == list(demo_report.recommendations)


def test_reports_mark_the_agents_contribution(demo_report, plan_kwargs):
    sink, kw = plan_kwargs
    _propose(kw, action="amend", target_rank=1, note="practical note")
    _propose(kw, action="replace", target_rank=2, title="Better",
             rationale="r", reason="cheaper", evidence="e")
    demo_report.investigation = {"fix_plan_edits": sink.as_dicts()}
    try:
        html = build_html_report(demo_report)
        md = render_markdown(demo_report)
    finally:
        demo_report.investigation = None
    assert "Agent&#x27;s practical note" in html or "Agent's practical note" in html
    assert "AI agent proposal, replaces #2" in html
    assert "superseded by #2" in html
    assert "The AI agent contributed to this plan" in html
    assert "[agent] practical note" in md
    assert "_(AI agent proposal, replaces #2)_" in md
    assert "_(superseded by #2)_" in md
    clean_html = build_html_report(demo_report)
    assert "AI agent proposal" not in clean_html
    assert "superseded" not in clean_html


def test_the_mcp_server_persists_edits_and_a_new_server_sees_them(
        demo_report, tmp_path, monkeypatch):
    triage = investigate.serialize_triage(
        demo_report.statistics, demo_report.selected_categories,
        demo_report.recommendations)
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints, None,
        adjacency={}, triage=triage)
    ev_path = tmp_path / "evidence.json"
    ev_path.write_text(json.dumps(evidence))
    monkeypatch.setenv(session.SESSION_DIR_ENV, str(tmp_path))

    first = mcp_server.load_state(str(ev_path))
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "propose_fix", "arguments": {
            "action": "amend", "target_rank": 1, "note": "from turn one"}}},
        first)
    assert json.loads(resp["result"]["content"][0]["text"])["recorded"]
    assert (tmp_path / fpe.FIX_EDITS_FILE).is_file()

    # A follow-up turn runs a fresh server process: it must start from what
    # the first turn recorded, not from an empty plan.
    second = mcp_server.load_state(str(ev_path))
    assert len(second["fix_edits"].items) == 1
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "recommend_fixes", "arguments": {"limit": 1}}},
        second)
    data = json.loads(resp["result"]["content"][0]["text"])
    assert data["agent_edits_applied"] == 1
    assert data["recommendations"][0]["agent_notes"] == ["[agent] from turn one"]

    sess = McpSession(work_dir=str(tmp_path), config_path="", evidence_path="")
    assert [e.note for e in sess.fix_edits()] == ["from turn one"]


def test_the_prompt_sends_the_review_into_the_plan():
    assert "propose_fix" in SYSTEM_PROMPT
    assert "propose_fix" in AGENTIC_SYSTEM_PROMPT
    assert "propose_fix" in investigate.TOOL_SPECS
    manager = SkillManager()
    assert any(s.skill_id == "propose_fix" for s in manager.skills)


def test_the_http_tool_loop_records_a_proposal_through_the_context(
        demo_report):
    sink = fpe.FixEditsSink()
    ctx = AnalysisContext(
        netlist=demo_report.netlist, faults=demo_report.faults,
        constraints=demo_report.constraints,
        fault_results=demo_report.fault_results,
        pattern_groups=demo_report.pattern_groups, summary=demo_report.summary,
        triage=investigate.serialize_triage(
            demo_report.statistics, demo_report.selected_categories,
            demo_report.recommendations),
        fix_edits=sink)

    class _Scripted(DebugAgent):
        def __init__(self):
            super().__init__(AgentConfig(backend="http", base_url="http://x",
                                         model="m"))
            self.rounds = 0

        def _post_chat(self, messages, tools=None):
            self.rounds += 1
            if self.rounds == 1:
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "propose_fix",
                        "arguments": json.dumps({
                            "action": "amend", "target_rank": 1,
                            "note": "via http loop"})}}]}
            return {"role": "assistant", "content": "done"}

    agent = _Scripted()
    agent.run_agentic(demo_report, SkillManager(), ctx)
    assert [e.note for e in sink.items] == ["via http loop"]


# ---------------------------------------------------------------------------
# Stopping a turn
# ---------------------------------------------------------------------------
def _fake_cli(tmp_path, body: str) -> str:
    script = tmp_path / "fake_cli.py"
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_stop_terminates_the_cli_and_keeps_the_partial_output(tmp_path):
    exe = _fake_cli(tmp_path,
                    "import sys, time\n"
                    "sys.stdout.write('partial answer'); sys.stdout.flush()\n"
                    "time.sleep(30)\n")
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=exe))
    threading.Timer(0.7, agent.cancel.cancel).start()
    started = time.monotonic()
    with pytest.raises(AgentCancelled) as info:
        agent._call_cli("system", "payload")
    assert time.monotonic() - started < 15, "the sleep must not be waited out"
    assert "partial answer" in str(info.value)


def test_a_stop_before_the_call_starts_does_not_launch_anything(tmp_path):
    marker = tmp_path / "launched"
    exe = _fake_cli(tmp_path, f"open({str(marker)!r}, 'w').write('x')\n")
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=exe))
    agent.cancel.cancel()
    with pytest.raises(AgentCancelled):
        agent._call_cli("system", "payload")
    assert not marker.exists()


def test_a_normal_cli_run_still_completes(tmp_path):
    exe = _fake_cli(tmp_path, "print('whole answer')\n")
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=exe))
    assert agent._call_cli("system", "payload") == "whole answer"
    chunks = []
    assert agent._call_cli("system", "payload",
                           on_chunk=chunks.append) == "whole answer"
    assert "".join(chunks).strip() == "whole answer"


def test_the_http_tool_loop_stops_between_rounds(demo_report):
    class _Scripted(DebugAgent):
        def __init__(self):
            super().__init__(AgentConfig(backend="http", base_url="http://x",
                                         model="m"))
            self.rounds = 0

        def _post_chat(self, messages, tools=None):
            self.rounds += 1
            # Stop is pressed while the first tool result is being produced.
            self.cancel.cancel()
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "coverage_triage",
                                          "arguments": "{}"}}]}

    ctx = AnalysisContext(
        netlist=None, faults=[], constraints=[], fault_results=[],
        pattern_groups=[], summary=demo_report.summary, triage={})
    agent = _Scripted()
    with pytest.raises(AgentCancelled):
        agent.run_agentic(demo_report, SkillManager(), ctx)
    assert agent.rounds == 1, "no second model round after the stop"


def test_the_guardrail_correction_is_skipped_after_a_stop(demo_report):
    class _Counting(DebugAgent):
        def __init__(self):
            super().__init__(AgentConfig(backend="http", base_url="http://x",
                                         model="m"))
            self.corrections = 0

        def run_with_prompt(self, system_prompt, user_payload):
            self.corrections += 1
            return "corrected"

    agent = _Counting()
    agent.cancel.cancel()
    bad = "This will recover 12% coverage."
    assert agent.correct_guardrail_issues(bad, demo_report) == bad
    assert agent.corrections == 0


def test_cancel_token_kills_an_attached_process():
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token = CancelToken()
    token.attach(proc)
    token.cancel()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("cancel() did not terminate the attached process")
    assert token.cancelled
    # Attaching a process after the stop terminates it immediately too.
    late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token.attach(late)
    try:
        late.wait(timeout=10)
    except subprocess.TimeoutExpired:
        late.kill()
        pytest.fail("a late attach must be terminated as well")
