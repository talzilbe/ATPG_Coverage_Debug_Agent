"""Right-click a path in the agent's text -> Tessent Visualizer; Save chat.

The menu is tested through its MODEL, never by executing a real QMenu: a
modal QMenu.exec is a C++ slot that monkeypatch cannot replace, and it would
block pytest forever.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from urllib.parse import quote

from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.analysis.guardrails import (
    PathRegistry,
    find_paths,
    scan_paths,
)
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def report(sample_netlist_path, sample_faults_path, sample_constraints_path):
    return run_analysis(sample_netlist_path, sample_faults_path,
                        sample_constraints_path)


@pytest.fixture
def panel(qapp, report):
    widget = AgentPanel()
    widget.set_report(report, None)
    yield widget
    widget.shutdown()


def _known_fault(report) -> str:
    return report.fault_results[0].fault.fault_object


# ---------------------------------------------------------------------------
# guardrails.find_paths — the shared tokenizer
# ---------------------------------------------------------------------------

def test_find_paths_reports_spans_and_strips_punctuation():
    text = "The loss sits at top/u_alu/U1/Y, then top/u_ctl/U9/A."
    found = find_paths(text)
    tokens = [t for t, _s, _e in found]
    assert tokens == ["top/u_alu/U1/Y", "top/u_ctl/U9/A"]
    for token, start, end in found:
        assert text[start:end] == token


def test_find_paths_skips_value_codes_placeholders_and_ellipsis():
    text = ("constrained to C0/C1 and the sa0/sa1 split; template "
            "<output_dir>/x.log; shortened top/.../U1/Y; real top/u_alu/U1/Y")
    tokens = [t for t, _s, _e in find_paths(text)]
    assert tokens == ["top/u_alu/U1/Y"]


def test_find_paths_reports_each_token_once():
    tokens = [t for t, _s, _e in find_paths("a/b/c then a/b/c again")]
    assert tokens == ["a/b/c"]


def test_find_paths_keeps_a_leading_separator_verbatim():
    # Copy-exact: what the menu offers must be pasteable into the tool as-is.
    tokens = [t for t, _s, _e in find_paths("the site /top/u_alu/U1/Y is tied")]
    assert tokens == ["/top/u_alu/U1/Y"]


def test_scan_paths_still_flags_unknown_paths(report):
    registry = PathRegistry.from_report(report)
    issues = scan_paths(f"see {_known_fault(report)} and made/up/path", registry)
    assert [i.text for i in issues] == ["made/up/path"]
    assert issues[0].kind == "unknown_path"


# ---------------------------------------------------------------------------
# AgentPanel menu model
# ---------------------------------------------------------------------------

def test_cursor_on_a_known_path_offers_it_enabled(panel, report):
    fo = _known_fault(report)
    text = f"Coverage is lost at {fo} because of a tie."
    pos = text.index(fo) + 3
    primary, rows = panel._text_menu_model(text, pos)
    assert primary == fo
    assert rows[0] == (f"Open {fo} in Tessent Visualizer", True, f"show:{fo}")
    assert ("Copy path", True, f"copy:{fo}") in rows


def test_cursor_on_an_unknown_path_keeps_the_row_but_disabled(panel):
    text = "The agent invented made/up/inst/Q here."
    pos = text.index("made/up") + 2
    primary, rows = panel._text_menu_model(text, pos)
    assert primary == "made/up/inst/Q"
    label, enabled, action_id = rows[0]
    assert enabled is False
    assert action_id == "show:made/up/inst/Q"
    # Copying it is still allowed: the user may want to look it up by hand.
    assert ("Copy path", True, "copy:made/up/inst/Q") in rows


def test_a_fault_anchor_under_the_cursor_wins(panel, report):
    fo = _known_fault(report)
    href = "fault:" + quote(fo, safe="")
    primary, rows = panel._text_menu_model("unrelated prose", 3, href)
    assert primary == fo
    assert rows[0][1] is True
    assert rows[0][2] == f"show:{fo}"


def test_prose_without_paths_offers_nothing(panel):
    primary, rows = panel._text_menu_model("No hierarchy here, just words.", 5)
    assert primary == ""
    assert rows == []


def test_cursor_on_prose_lists_every_path_in_the_paragraph(panel, report):
    fo = _known_fault(report)
    text = f"Compare {fo} with top/u_ctl/U9/A and fake/x/y."
    primary, rows = panel._text_menu_model(text, 2)
    assert primary == ""
    shows = [(r[2], r[1]) for r in rows if r[2].startswith("show:")]
    assert shows == [(f"show:{fo}", True), ("show:top/u_ctl/U9/A", False),
                     ("show:fake/x/y", False)]
    copies = [r for r in rows if r[2].startswith("copy:")]
    assert len(copies) == 3
    assert all(r[0].startswith("Copy path ") for r in copies)


def test_the_paragraph_listing_is_capped(panel):
    text = " ".join(f"a{i}/b/c" for i in range(20))
    _primary, rows = panel._text_menu_model(text, 0)
    # Position 0 sits on the first token, so force the prose case.
    _primary, rows = panel._text_menu_model("x " + text, 0)
    shows = [r for r in rows if r[2].startswith("show:")]
    assert len(shows) == AgentPanel.MAX_MENU_PATHS


def test_show_action_emits_the_object_verbatim(panel):
    got = []
    panel.signal_inspect_requested.connect(got.append)
    panel._run_text_menu_action("show:top/u_alu/U1/Y")
    assert got == ["top/u_alu/U1/Y"]


def test_copy_action_puts_the_path_on_the_clipboard(panel, qapp):
    panel._run_text_menu_action("copy:top/u_alu/U1/Y")
    assert qapp.clipboard().text() == "top/u_alu/U1/Y"


def test_registry_is_rebuilt_when_the_report_changes(panel, report):
    assert panel._is_known_path(_known_fault(report))
    panel.set_report(None, None)
    assert panel._path_registry is None
    assert not panel._is_known_path(_known_fault(report))


def test_both_text_views_use_a_custom_context_menu(panel):
    from PySide6.QtCore import Qt
    assert panel.chat_view.contextMenuPolicy() == Qt.CustomContextMenu
    assert panel.response_view.contextMenuPolicy() == Qt.CustomContextMenu


# ---------------------------------------------------------------------------
# main_window wiring
# ---------------------------------------------------------------------------

def test_main_window_routes_the_agent_signal_to_the_visualizer(qapp, report,
                                                                monkeypatch):
    from atpg_coverage_debug_agent.gui.main_window import MainWindow
    win = MainWindow()
    try:
        win._apply_report(report)
        shown = []
        monkeypatch.setattr(win.visualizer_panel, "show_signal",
                            lambda obj, action=None: shown.append(obj) or False)
        win.agent_panel.signal_inspect_requested.emit("top/u_alu/U1/Y")
        assert shown == ["top/u_alu/U1/Y"]
        # A refusal moves the user to where the reason is displayed.
        assert win.tabs.tabText(win.tabs.currentIndex()) == "Tessent Visualizer"
    finally:
        win.agent_panel.shutdown()


def test_help_names_the_agent_text_as_a_place_to_open_signals():
    from atpg_coverage_debug_agent.gui.main_window import _HELP_HTML
    assert "Follow-up\n      Chat</b> text" in _HELP_HTML or \
        "Follow-up Chat</b> text" in _HELP_HTML
    assert "Save chat" in _HELP_HTML


# ---------------------------------------------------------------------------
# Save chat
# ---------------------------------------------------------------------------

def test_empty_conversation_has_no_transcript(panel):
    assert panel.chat_transcript() == ""


def test_transcript_carries_diagnosis_and_turns_in_order(panel):
    panel._chat_backend = "cli"
    panel._set_response("A. Verdict: the loss is a tie.")
    panel._append_chat("You", "Which module?")
    panel._append_chat("Agent", "top/u_alu carries most of it.")
    panel._append_chat("You", "And the fix?")
    md = panel.chat_transcript()
    assert md.startswith("# ATPG Coverage Debug Agent")
    assert "- Backend: cli" in md
    assert "## Initial diagnosis" in md
    assert "## Follow-up conversation" in md
    order = [md.index(s) for s in (
        "## Initial diagnosis", "the loss is a tie", "## Follow-up conversation",
        "### You", "Which module?", "### Agent", "top/u_alu carries",
        "And the fix?")]
    assert order == sorted(order)


def test_transcript_survives_clear_chat(panel):
    panel._append_chat("You", "first question")
    panel._append_chat("Agent", "first answer")
    panel.on_clear_chat()
    assert panel.chat_view.toPlainText().strip() == ""
    md = panel.chat_transcript()
    assert "first question" in md and "first answer" in md


def test_transcript_without_follow_ups_says_so(panel):
    panel._set_response("Diagnosis only.")
    md = panel.chat_transcript()
    assert "Diagnosis only." in md
    assert "(no follow-up questions were asked)" in md


def test_save_chat_writes_the_transcript(panel, tmp_path, monkeypatch):
    from atpg_coverage_debug_agent.gui import agent_panel as mod
    target = tmp_path / "chat.md"
    monkeypatch.setattr(mod.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    panel._set_response("Saved diagnosis.")
    panel._append_chat("You", "hello")
    panel.on_save_chat()
    text = target.read_text(encoding="utf-8")
    assert "Saved diagnosis." in text and "### You" in text
    assert panel.status_label.text().startswith("Saved:")


def test_save_chat_with_nothing_to_save_refuses(panel, monkeypatch):
    from atpg_coverage_debug_agent.gui import agent_panel as mod
    called = []
    monkeypatch.setattr(mod.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: called.append(1) or ("", "")))
    panel.on_save_chat()
    assert called == []
    assert panel.status_label.text() == "Nothing to save."
