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

