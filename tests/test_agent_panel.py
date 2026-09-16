"""Offscreen tests for the agent panel's grounded-evidence and verify features."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from urllib.parse import quote

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel_with_report(qapp, sample_netlist_path, sample_faults_path,
                      sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    panel = AgentPanel()
    panel.set_report(rep, None)
    yield panel, rep
    # Constructing the panel schedules a Copilot CLI model fetch on the next
    # event-loop turn. Any test that pumps events starts that thread for real,
    # and Qt aborts the process if it outlives its wrapper.
    panel.shutdown()


def test_linkify_creates_fault_anchor(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    html = panel._to_html(f"Coverage lost at {fo} here.")
    assert 'href="fault:' in html
    assert quote(fo, safe="") in html


def test_anchor_click_emits_fault(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    captured = []
    panel.fault_referenced.connect(captured.append)
    panel._on_anchor_clicked(QUrl("fault:" + quote(fo, safe="")))
    assert captured == [fo]


def test_verify_flags_grounded_and_hallucinated(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    panel._last_response = f"Faults {fo} and top/made/up/XX are affected."
    panel.on_verify()
    trace = panel.trace_view.toPlainText()
    assert "VERIFICATION" in trace
    assert fo in trace                     # grounded, with ground-truth attrs
    assert "top/made/up/XX" in trace       # flagged as not-in-report


def test_ask_about_fault_prefills_chat(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    panel.ask_about_fault(fo)
    assert fo in panel.chat_input.text()


def test_response_token_streams_then_finalizes(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    panel.response_view.clear()
    panel._stream_buf = ""
    panel._on_response_token("Coverage lost at ")
    panel._on_response_token(fo)
    assert panel._stream_buf == f"Coverage lost at {fo}"
    assert fo in panel.response_view.toPlainText()
    # Finalizing re-renders with clickable links.
    panel._on_finished("")  # empty -> uses the streamed buffer
    assert 'href="fault:' in panel.response_view.toHtml()


def test_chat_token_streaming_rebuilds_transcript(panel_with_report):
    panel, rep = panel_with_report
    panel._chat_backend = "cli"
    panel._chat_turns = []
    panel.chat_view.clear()
    panel._append_chat("You", "why?")
    panel._chat_stream_buf = ""
    panel._on_chat_token("because ")
    panel._on_chat_token("of a constraint")
    panel._on_chat_finished("")  # empty -> uses streamed buffer
    text = panel.chat_view.toPlainText()
    assert "why?" in text
    assert "because of a constraint" in text
    assert panel._chat_turns[-1][0] == "Agent"


# ---------------------------------------------------------------------------
# Guardrail auditing
# ---------------------------------------------------------------------------
def test_the_first_answer_is_audited(panel_with_report):
    panel, _rep = panel_with_report
    panel._on_finished("This change will recover 12% coverage.")
    assert "Guardrail check" in panel.response_view.toPlainText()


def test_follow_up_answers_are_audited_too(panel_with_report):
    """A follow-up is the likelier place for an unmeasured claim.

    "So how much would that recover?" is the natural next question, so an
    unaudited chat reply is exactly where a fabricated percentage lands.
    """
    panel, _rep = panel_with_report
    panel._chat_backend = "cli"
    panel._chat_turns = []
    panel.chat_view.clear()
    panel._append_chat("You", "how much would that recover?")
    panel._chat_stream_buf = ""

    panel._on_chat_finished("It will recover 12% coverage.")

    assert "Guardrail check" in panel.chat_view.toPlainText()
    assert "Guardrail check" in panel._chat_turns[-1][1]


def test_a_clean_follow_up_gets_no_notice(panel_with_report):
    panel, _rep = panel_with_report
    panel._chat_backend = "cli"
    panel._chat_turns = []
    panel.chat_view.clear()
    panel._chat_stream_buf = ""
    panel._on_chat_finished("The constraint on pi_hold blocks activation.")
    assert "Guardrail check" not in panel.chat_view.toPlainText()


def test_chat_transcript_is_left_to_right_and_one_block_per_turn(
        panel_with_report, qapp):
    from PySide6.QtCore import Qt

    panel, _rep = panel_with_report
    qapp.setLayoutDirection(Qt.RightToLeft)   # hostile (RTL) system locale
    try:
        panel._chat_turns = []
        panel.chat_view.clear()
        panel._append_chat("You", "why is AU.PC high?")
        panel._append_chat("Agent", "the scan enable is tied off")

        assert panel.chat_view.layoutDirection() == Qt.LeftToRight

        doc = panel.chat_view.document()
        blocks = [doc.findBlockByNumber(i)
                  for i in range(doc.blockCount())]
        texts = [b.text() for b in blocks if b.text().strip()]
        # One paragraph per turn, speaker label first, on the same line.
        assert texts == ["You: why is AU.PC high?",
                         "Agent: the scan enable is tied off"]
        for b in blocks:
            assert b.textDirection() == Qt.LeftToRight
            assert b.blockFormat().alignment() & Qt.AlignLeft
    finally:
        qapp.setLayoutDirection(Qt.LeftToRight)



def test_investigation_export_import(panel_with_report, qapp,
                                     sample_netlist_path, sample_faults_path,
                                     sample_constraints_path):
    panel, rep = panel_with_report
    panel._set_response("Diagnosis text about a fault.")
    panel._append_chat("You", "why is it lost?")
    panel._append_chat("Agent", "because of a constraint")
    panel.trace_view.setPlainText("=== VERIFICATION ===")

    data = panel.export_investigation()
    assert data["diagnosis"].startswith("Diagnosis text")
    assert [t["role"] for t in data["chat"]] == ["You", "Agent"]
    assert "VERIFICATION" in data["trace"]

    # Import into a fresh panel restores the transcript.
    fresh = AgentPanel()
    fresh.set_report(rep, None)
    fresh.import_investigation(data)
    assert "Diagnosis text" in fresh.response_view.toPlainText()
    chat_text = fresh.chat_view.toPlainText()
    assert "why is it lost?" in chat_text
    assert "because of a constraint" in chat_text
    assert "VERIFICATION" in fresh.trace_view.toPlainText()

    # Importing None clears everything.
    fresh.import_investigation(None)
    assert fresh.response_view.toPlainText().strip() == ""
    assert fresh.chat_view.toPlainText().strip() == ""


# ---------------------------------------------------------------------------
# Fault-row cap: truncation must be visible, not silent
# ---------------------------------------------------------------------------
@pytest.fixture
def panel_with_demo(qapp):
    """A panel over the demo dataset, which has enough faults to truncate.

    The minimal sample_* fixtures carry fewer faults than the spinner's floor
    of 10, so the cap can never bite there.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = os.path.join(here, "sample_data")
    rep = run_analysis(os.path.join(data, "demo_netlist.v"),
                       os.path.join(data, "demo_faults.mtfi"),
                       os.path.join(data, "demo_constraints.do"))
    panel = AgentPanel()
    panel.set_report(rep, None)
    yield panel, rep
    panel.shutdown()


def test_hint_reports_when_every_fault_row_fits(panel_with_demo):
    panel, rep = panel_with_demo
    total = len(rep.fault_results)

    panel.maxfaults_spin.setValue(total)

    assert f"all {total:,} rows" in panel.maxfaults_hint.text()
    assert "omitted" not in panel.maxfaults_hint.text()


def test_hint_warns_when_fault_rows_are_dropped(panel_with_demo):
    """The payload only says '... N more faults omitted', which is easy to miss."""
    panel, rep = panel_with_demo
    total = len(rep.fault_results)
    cap = total // 2
    assert cap >= panel.maxfaults_spin.minimum(), "fixture must allow a cut"

    panel.maxfaults_spin.setValue(cap)

    text = panel.maxfaults_hint.text()
    assert "omitted" in text
    assert f"{total - cap:,}" in text
    # The tooltip must say the aggregate analysis is still complete, so the
    # warning is not read as "the agent only saw half the design".
    tip = panel.maxfaults_hint.toolTip()
    assert "triage still cover all" in tip
    assert "Agentic" in tip


def test_hint_is_empty_before_an_analysis_exists(qapp):
    panel = AgentPanel()
    try:
        assert panel.maxfaults_hint.text() == ""
    finally:
        panel.shutdown()


def test_cap_limits_the_table_but_not_the_summary_or_triage(panel_with_demo):
    """The knob must never cut the aggregate analysis, only the row dump."""
    from atpg_coverage_debug_agent.agent.debug_agent import build_user_payload

    _panel, rep = panel_with_demo
    payload = build_user_payload(rep, max_faults=1)

    assert "more faults omitted" in payload
    assert "## Summary" in payload
    assert "## Evidence Basis of the Coverage Loss" in payload
    assert "## Offline Triage Conclusions (report section S4)" in payload
    assert "## Ranked Fix Plan (report section S5)" in payload
    # Whole-population figures survive the cap.
    assert str(rep.summary.coverage_loss_count) in payload


# ---------------------------------------------------------------------------
# "Agent is working" indicator
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (9, "9s"),
    (59, "59s"),
    (60, "1m 00s"),
    (135, "2m 15s"),
    (3600, "1h 00m 00s"),
    (3725, "1h 02m 05s"),
])
def test_elapsed_is_formatted_like_a_stopwatch(seconds, expected):
    assert AgentPanel._format_elapsed(seconds) == expected


def test_busy_indicator_animates_and_counts(panel_with_report):
    """A long call must not look like a hang, so the status line moves."""
    panel, _rep = panel_with_report

    panel._start_busy("Calling the LLM")
    assert panel._busy_timer.isActive()
    first = panel.status_label.text()
    panel._tick_busy()
    second = panel.status_label.text()

    assert first != second, "the spinner must advance between ticks"
    assert first[0] != second[0]
    for text in (first, second):
        assert "Calling the LLM" in text
        assert text.rstrip().endswith("s")   # the elapsed count

    panel._stop_busy()
    assert not panel._busy_timer.isActive()


def test_busy_indicator_stops_on_every_completion_path(panel_with_report):
    panel, _rep = panel_with_report

    panel._start_busy("Calling the LLM")
    panel._on_finished("done")
    assert not panel._busy_timer.isActive()
    assert "received in" in panel.status_label.text()

    panel._start_busy("Calling the LLM")
    panel._on_failed("boom")
    assert not panel._busy_timer.isActive()

    panel._chat_backend = "cli"
    panel._chat_turns = []
    panel._start_busy("Agent is replying")
    panel._on_chat_finished("reply")
    assert not panel._busy_timer.isActive()
    assert "Reply received in" in panel.status_label.text()

    panel._start_busy("Agent is replying")
    panel._on_chat_failed("nope")
    assert not panel._busy_timer.isActive()


# ---------------------------------------------------------------------------
# Pop-out windows behave like ordinary desktop windows
# ---------------------------------------------------------------------------
@pytest.fixture
def popout(panel_with_report):
    panel, _rep = panel_with_report
    box = panel._splitter_boxes[0]
    panel._toggle_popout(box, "Assembled Prompt")
    dlg, _btn = panel._popouts[box]
    yield panel, box, dlg
    if box in panel._popouts:
        panel._toggle_popout(box, "Assembled Prompt")


# ---------------------------------------------------------------------------
# The chat box carries its own busy indicator (for when it is popped out)
# ---------------------------------------------------------------------------
def test_chat_busy_indicator_is_mirrored_into_the_chat_box(panel_with_report):
    panel, _rep = panel_with_report
    assert not panel.chat_status_label.isVisibleTo(panel)

    panel._start_busy("Agent is replying", chat=True)
    assert panel.chat_status_label.isVisibleTo(panel)
    first = panel.chat_status_label.text()
    panel._tick_busy()
    second = panel.chat_status_label.text()
    assert first != second and "Agent is replying" in second
    assert second == panel.status_label.text(), "same line in both places"

    panel._chat_backend = "cli"
    panel._chat_turns = []
    panel._on_chat_finished("reply")
    assert not panel.chat_status_label.isVisibleTo(panel)
    assert panel.chat_status_label.text() == ""


def test_the_chat_indicator_lives_inside_the_popped_out_chat_window(
        panel_with_report):
    """The whole point: when the chat box is in its own window, the busy
    line must travel with it, not stay behind on the main tab."""
    panel, _rep = panel_with_report
    box = panel._chat_box
    panel._toggle_popout(box, "Follow-up Chat with the Agent")
    try:
        dlg, _btn = panel._popouts[box]
        assert panel.chat_status_label.window() is dlg
        assert panel.status_label.window() is not dlg
        panel._start_busy("Agent is replying", chat=True)
        assert panel.chat_status_label.isVisibleTo(dlg)
        assert "Agent is replying" in panel.chat_status_label.text()
        panel._stop_busy()
    finally:
        if box in panel._popouts:
            panel._toggle_popout(box, "Follow-up Chat with the Agent")
    assert panel.chat_status_label.window() is panel.window()


def test_a_main_run_does_not_light_the_chat_indicator(panel_with_report):
    panel, _rep = panel_with_report
    panel._start_busy("Calling the LLM")
    assert not panel.chat_status_label.isVisibleTo(panel)
    panel._stop_busy()


def test_on_send_chat_uses_the_chat_indicator(panel_with_report, monkeypatch):
    panel, _rep = panel_with_report
    from PySide6.QtCore import QThread
    started = []
    monkeypatch.setattr(QThread, "start", lambda self, *a: started.append(1))
    panel._session_id = "sid"
    panel._chat_backend = "cli"
    panel.cli_path_edit.setText(__file__)  # any existing file "configures" it
    panel._set_chat_enabled(True)
    panel.chat_input.setText("why?")
    panel.on_send_chat()
    assert started, "a chat worker thread was set up"
    assert panel._busy_chat is True
    assert panel.chat_status_label.isVisibleTo(panel)
    panel._stop_busy()
    panel._chat_thread = None


# ---------------------------------------------------------------------------
# Findings and the tool-call log reach the panel
# ---------------------------------------------------------------------------
def test_findings_are_collected_persisted_and_restored(panel_with_report,
                                                       tmp_path):
    from atpg_coverage_debug_agent.agent.debug_agent import McpSession
    from atpg_coverage_debug_agent.analysis.findings import (
        Finding,
        FindingsSink,
    )
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object

    # CLI route: findings written by the MCP server into the session dir.
    sess = McpSession(work_dir=str(tmp_path), config_path="", evidence_path="")
    FindingsSink(sess.findings_path).record(
        Finding(kind="correction", subject=fo, field="root_cause",
                agent_value="tied", evidence="why_blocked"))
    # HTTP route: the in-process sink.
    panel._findings_sink = FindingsSink()
    panel._findings_sink.record(Finding(kind="gap", subject="AU.TC"))
    panel._mcp_session = sess

    emitted = []
    panel.findings_changed.connect(emitted.append)
    panel._collect_findings()
    kinds = sorted(f["kind"] for f in panel.current_findings())
    assert kinds == ["correction", "gap"]
    assert emitted and len(emitted[-1]) == 2
    assert "structured finding(s) recorded" in panel.trace_view.toPlainText()

    # Collecting again does not duplicate.
    panel._collect_findings()
    assert len(panel.current_findings()) == 2

    data = panel.export_investigation()
    assert len(data["findings"]) == 2
    fresh = AgentPanel()
    try:
        fresh.set_report(rep, None)
        fresh.import_investigation(data)
        assert len(fresh.current_findings()) == 2
        fresh.import_investigation(None)
        assert fresh.current_findings() == []
    finally:
        fresh.shutdown()
    panel._mcp_session = None


def test_tool_calls_logged_by_the_server_are_tailed_into_the_trace(
        panel_with_report, tmp_path):
    import json
    from atpg_coverage_debug_agent.agent.debug_agent import McpSession
    panel, _rep = panel_with_report
    sess = McpSession(work_dir=str(tmp_path), config_path="", evidence_path="")
    panel._mcp_session = sess
    panel._tool_log_offset = 0
    panel._tool_log_calls = 0
    with open(sess.tool_log_path, "w") as fh:
        fh.write(json.dumps({"ts": "01:02:03", "tool": "list_faults",
                             "args": {"limit": 2}, "ok": True, "chars": 10,
                             "truncated": False, "ms": 1.5}) + "\n")
    panel._poll_tool_log()
    text = panel.trace_view.toPlainText()
    assert "MCP tool call #1: list_faults(limit=2)" in text

    # Only NEW lines are appended on the next poll.
    with open(sess.tool_log_path, "a") as fh:
        fh.write(json.dumps({"ts": "01:02:04", "tool": "scan_status",
                             "args": {"target": "x"}, "ok": False, "chars": 0,
                             "truncated": True, "ms": 2}) + "\n")
    panel._poll_tool_log()
    text = panel.trace_view.toPlainText()
    assert text.count("list_faults(limit=2)") == 1
    assert "MCP tool call #2: scan_status(target=x)" in text
    assert "error" in text and "truncated" in text
    panel._mcp_session = None


def test_a_new_session_closes_the_previous_mcp_session(panel_with_report,
                                                       tmp_path):
    from atpg_coverage_debug_agent import session
    from atpg_coverage_debug_agent.agent.debug_agent import (
        AgentConfig,
        McpSession,
    )
    panel, _rep = panel_with_report
    work = session.session_dir("panel_test", "run-x", reuse_env=False)
    cfg = os.path.join(work, "mcp-config.json")
    open(cfg, "w").write("{}")
    panel._mcp_session = McpSession(work_dir=work, config_path=cfg,
                                    evidence_path="")
    panel._begin_session(AgentConfig(backend="cli", cli_path=__file__),
                         agentic=True)
    assert panel._mcp_session is None
    assert not os.path.isdir(work)
    assert panel._findings_sink is not None and panel._chat_agentic is True


def test_prefilled_question_only_names_tools_when_the_chat_has_them(
        panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    panel._chat_agentic = False
    panel.ask_about_fault(fo)
    assert "why_blocked" not in panel.chat_input.text()
    panel._chat_agentic = True
    panel.ask_about_fault(fo)
    assert "why_blocked" in panel.chat_input.text()


# ---------------------------------------------------------------------------
# Stop: the turn ends, the conversation survives
# ---------------------------------------------------------------------------
def test_stop_buttons_are_disabled_until_a_turn_runs(panel_with_report):
    panel, _rep = panel_with_report
    assert not panel.stop_btn.isEnabled()
    assert not panel.chat_stop_btn.isEnabled()
    panel.on_stop()
    assert "Nothing is running" in panel.status_label.text()


def test_the_chat_row_stop_lives_in_the_chat_box(panel_with_report):
    panel, _rep = panel_with_report
    box = panel._chat_box
    panel._toggle_popout(box, "Follow-up Chat with the Agent")
    try:
        dlg, _btn = panel._popouts[box]
        assert panel.chat_stop_btn.window() is dlg
    finally:
        if box in panel._popouts:
            panel._toggle_popout(box, "Follow-up Chat with the Agent")


def test_sending_a_chat_enables_stop_and_stop_cancels_the_worker(
        panel_with_report, monkeypatch):
    panel, _rep = panel_with_report
    from PySide6.QtCore import QThread
    monkeypatch.setattr(QThread, "start", lambda self, *a: None)
    panel._session_id = "sid"
    panel._chat_backend = "cli"
    panel.cli_path_edit.setText(__file__)
    panel._set_chat_enabled(True)
    panel.chat_input.setText("why?")
    panel.on_send_chat()
    assert panel.stop_btn.isEnabled() and panel.chat_stop_btn.isEnabled()
    worker = panel._chat_worker
    assert worker is not None and not worker._agent.cancel.cancelled
    panel.on_stop()
    assert worker._agent.cancel.cancelled
    assert not panel.stop_btn.isEnabled()
    assert "Stopping" in panel._busy_message
    panel._on_chat_cancelled("half a reply")
    panel._chat_thread = None
    panel._chat_worker = None


def test_a_stopped_run_keeps_the_partial_answer_and_the_conversation(
        panel_with_report):
    panel, _rep = panel_with_report
    panel._chat_backend = "cli"
    panel._chat_agentic = False
    panel._start_busy("Calling the LLM")
    panel._set_stop_enabled(True)
    panel.run_btn.setEnabled(False)
    panel._on_cancelled("first half of the diagnosis")
    text = panel.response_view.toPlainText()
    assert "first half of the diagnosis" in text
    assert "Stopped by user" in text and "PARTIAL" in text
    assert not panel._busy_timer.isActive()
    assert panel.run_btn.isEnabled()
    assert not panel.stop_btn.isEnabled()
    assert panel.chat_input.isEnabled(), "the conversation stays open"
    assert "Stopped by user after" in panel.status_label.text()
    assert "stopped by user" in panel.chat_view.toPlainText()


def test_a_stop_with_no_output_says_so(panel_with_report):
    panel, _rep = panel_with_report
    panel._chat_backend = "cli"
    panel._stream_buf = ""
    panel._start_busy("Calling the LLM")
    panel._on_cancelled("")
    assert "before any output" in panel.response_view.toPlainText()


def test_a_stopped_chat_reply_is_kept_as_a_turn(panel_with_report):
    panel, _rep = panel_with_report
    panel._chat_backend = "http"
    panel._chat_messages = []
    panel._chat_turns = [("You", "why?")]
    panel._start_busy("Agent is replying", chat=True)
    panel._on_chat_cancelled("partial reply")
    assert panel._chat_turns[-1][0] == "Agent"
    assert "partial reply" in panel._chat_turns[-1][1]
    assert "stopped by user" in panel._chat_turns[-1][1]
    assert panel._chat_messages[-1] == {"role": "assistant",
                                        "content": "partial reply"}
    assert panel.chat_input.isEnabled()
    assert not panel.chat_status_label.isVisibleTo(panel)
    assert "Reply stopped after" in panel.status_label.text()


def test_worker_reports_a_cancel_as_cancelled_not_failed(panel_with_report):
    from atpg_coverage_debug_agent.agent.debug_agent import AgentCancelled
    from atpg_coverage_debug_agent.gui.agent_panel import _AgentWorker

    class _Agent:
        class cancel:  # noqa: N801 - mimics DebugAgent.cancel
            @staticmethod
            def cancel():
                pass

        def run(self, report, session_id=None, on_chunk=None):
            raise AgentCancelled("partial")

    worker = _AgentWorker(_Agent(), None)
    got = {}
    worker.cancelled.connect(lambda t: got.setdefault("cancelled", t))
    worker.failed.connect(lambda t: got.setdefault("failed", t))
    worker.run()
    assert got == {"cancelled": "partial"}


# ---------------------------------------------------------------------------
# Fix-plan edits reach the panel and the triage tab
# ---------------------------------------------------------------------------
def test_fix_plan_edits_are_collected_and_persisted(panel_with_report,
                                                    tmp_path):
    from atpg_coverage_debug_agent.agent.debug_agent import McpSession
    from atpg_coverage_debug_agent.analysis.fix_plan_edits import (
        FixEditsSink,
        FixPlanEdit,
    )
    panel, rep = panel_with_report
    sess = McpSession(work_dir=str(tmp_path), config_path="", evidence_path="")
    FixEditsSink(sess.fix_edits_path).record(
        FixPlanEdit(action="amend", subclass="UO", target_rank=1, note="n"))
    panel._fix_edits_sink = FixEditsSink()
    panel._fix_edits_sink.record(
        FixPlanEdit(action="add", subclass="UO", title="T", rationale="r",
                    evidence="e"))
    panel._mcp_session = sess

    emitted = []
    panel.fix_plan_changed.connect(emitted.append)
    panel._collect_fix_edits()
    assert sorted(e["action"] for e in panel.current_fix_edits()) == \
        ["add", "amend"]
    assert emitted and len(emitted[-1]) == 2
    assert "fix-plan edit(s) recorded" in panel.trace_view.toPlainText()
    panel._collect_fix_edits()
    assert len(panel.current_fix_edits()) == 2

    data = panel.export_investigation()
    assert len(data["fix_plan_edits"]) == 2
    fresh = AgentPanel()
    try:
        fresh.set_report(rep, None)
        fresh.import_investigation(data)
        assert len(fresh.current_fix_edits()) == 2
        fresh.import_investigation(None)
        assert fresh.current_fix_edits() == []
    finally:
        fresh.shutdown()
    panel._mcp_session = None


def test_triage_tab_shows_the_agents_edits(qapp):
    from atpg_coverage_debug_agent.gui.triage_panel import TriagePanel
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = os.path.join(here, "sample_data")
    rep = run_analysis(os.path.join(data, "demo_netlist.v"),
                       os.path.join(data, "demo_faults.mtfi"),
                       os.path.join(data, "demo_constraints.do"))
    base_count = len(rep.recommendations)
    tp = TriagePanel()
    tp.set_report(rep)
    assert tp.fix_list.count() == base_count

    rep.investigation = {"fix_plan_edits": [
        {"action": "amend", "subclass": rep.recommendations[0].subclass_id,
         "target_rank": 1, "note": "do the what-if first"},
        {"action": "replace", "subclass": rep.recommendations[1].subclass_id,
         "target_rank": 2, "title": "Better", "rationale": "r",
         "reason": "cheaper", "evidence": "e"},
    ]}
    tp.refresh_fix_plan()
    labels = [tp.fix_list.item(i).text() for i in range(tp.fix_list.count())]
    assert tp.fix_list.count() == base_count + 1
    assert labels[0].startswith("1. [amended]")
    assert "[agent] Better" in labels[1]
    assert labels[-1].startswith(f"{base_count + 1}. [superseded]")

    tp.fix_list.setCurrentRow(0)
    assert "do the what-if first" in tp.fix_detail.toPlainText()
    tp.fix_list.setCurrentRow(1)
    detail = tp.fix_detail.toPlainText()
    assert "Proposed by the AI agent" in detail and "cheaper" in detail
    tp.fix_list.setCurrentRow(tp.fix_list.count() - 1)
    assert "Superseded" in tp.fix_detail.toPlainText()


def test_popout_asks_for_real_window_controls(popout):
    """A plain QDialog frame often has no maximise button at all."""
    from PySide6.QtCore import Qt

    _panel, _box, dlg = popout
    flags = dlg.windowFlags()
    assert flags & Qt.Window
    assert flags & Qt.WindowMinMaxButtonsHint


def test_popout_maximize_and_full_screen_toggle(popout):
    _panel, _box, dlg = popout

    dlg.max_btn.click()
    assert dlg.isMaximized()
    assert "Restore down" in dlg.max_btn.text()
    dlg.max_btn.click()
    assert not dlg.isMaximized()
    assert "Maximize" in dlg.max_btn.text()

    dlg.full_btn.click()
    assert dlg.isFullScreen()
    assert "Leave full screen" in dlg.full_btn.text()
    dlg.full_btn.click()
    assert not dlg.isFullScreen()


def test_popout_binds_the_usual_shortcuts(popout):
    from PySide6.QtGui import QShortcut

    _panel, _box, dlg = popout
    bound = sorted(s.key().toString() for s in dlg.findChildren(QShortcut))
    assert bound == ["Ctrl+M", "F11"]


def _press_escape(widget) -> None:
    """Deliver a real Escape key press to *widget*, synchronously.

    ``QTest.keyClick`` would do the same, but nothing here pumps the event
    loop on purpose: the panel schedules a Copilot CLI model fetch with
    ``singleShot(0)`` when it is built, and pumping would launch that
    subprocess in every test.
    """
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    for kind in (QEvent.KeyPress, QEvent.KeyRelease):
        QApplication.sendEvent(
            widget, QKeyEvent(kind, Qt.Key_Escape, Qt.NoModifier))


def test_escape_leaves_full_screen_instead_of_closing(popout):
    """Qt rejects a dialog on Escape; in full screen that loses the window."""
    panel, box, dlg = popout
    dlg.full_btn.click()
    assert dlg.isFullScreen()

    _press_escape(dlg)

    assert not dlg.isFullScreen()
    assert box in panel._popouts, "Escape must not close a full-screen pop-out"
    assert "Full screen" in dlg.full_btn.text()


def test_escape_still_docks_back_when_not_full_screen(popout):
    panel, box, dlg = popout
    assert not dlg.isFullScreen()

    _press_escape(dlg)

    assert box not in panel._popouts
    assert panel._splitter.indexOf(box) != -1

