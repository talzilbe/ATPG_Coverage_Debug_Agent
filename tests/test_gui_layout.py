"""The first-time-user layout: what is shown, what is folded, how to get it back."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from atpg_coverage_debug_agent.app import run_analysis  # noqa: E402
from atpg_coverage_debug_agent.config import settings as settings_mod  # noqa: E402
from atpg_coverage_debug_agent.gui import main_window as mw  # noqa: E402
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel  # noqa: E402
from atpg_coverage_debug_agent.gui.triage_panel import TriagePanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "_DEFAULT_CONFIG_FILE",
                        tmp_path / "settings.json")
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    win = mw.MainWindow()
    yield win
    win.agent_panel.shutdown()
    win.close()


@pytest.fixture
def demo_report():
    demo = mw._demo_inputs()
    if demo is None:
        pytest.skip("demo data not present")
    return run_analysis(*demo)


def _show(win, report):
    win._base_report = report
    win._apply_report(report)
    win._after_new_report()


def _titles(win, visible_only=True):
    return [win.tabs.tabText(i) for i in range(win.tabs.count())
            if not visible_only or win.tabs.isTabVisible(i)]


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
def test_the_triage_tab_title_shows_its_ampersand(window):
    assert "Triage && Fix Plan" in _titles(window)


def test_a_new_user_sees_five_tabs(window):
    assert _titles(window) == ["Summary", "Triage && Fix Plan",
                               "Coverage Loss Table", "AI Debug Agent",
                               "Tessent Visualizer"]
    assert {"Logs / Warnings", "Skills", "Custom Skills"} <= set(
        _titles(window, visible_only=False))


def test_the_view_menu_brings_the_advanced_tabs_back(window):
    window.advanced_tabs_action.setChecked(True)
    assert {"Logs / Warnings", "Skills", "Custom Skills"} <= set(_titles(window))
    window.advanced_tabs_action.setChecked(False)
    assert "Skills" not in _titles(window)


def test_opening_one_advanced_tab_shows_and_selects_it(window):
    window._switch_to_tab("Logs / Warnings")
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Logs / Warnings"


def test_switching_by_the_plain_title_finds_the_triage_tab(window):
    window._switch_to_tab("Triage & Fix Plan")
    assert window.tabs.currentWidget() is window.triage_panel


def test_the_advanced_tab_choice_is_remembered(window, tmp_path):
    window.advanced_tabs_action.setChecked(True)
    reloaded = settings_mod.AppSettings.load(tmp_path / "settings.json")
    assert reloaded.show_advanced_tabs is True


def test_the_partition_queue_is_hidden_until_asked_for(window):
    assert not window.partition_box.isVisibleTo(window)
    window.partitions_toggle.setChecked(True)
    assert window.partition_box.isVisibleTo(window)
    assert "Hide" in window.partitions_toggle.text()


def test_exports_and_edits_wait_for_a_report(window, demo_report):
    for action in (window.md_action, window.csv_action,
                   window.compare_action, window.edit_action):
        assert not action.isEnabled()
    _show(window, demo_report)
    for action in (window.md_action, window.csv_action,
                   window.compare_action, window.edit_action):
        assert action.isEnabled()


def test_inputs_fold_after_a_report_and_come_back(window, demo_report):
    assert window.inputs_box.isVisibleTo(window)
    assert not window.inputs_summary.isVisibleTo(window)
    _show(window, demo_report)
    assert not window.inputs_box.isVisibleTo(window)
    assert window.inputs_summary.isVisibleTo(window)
    assert "demo_netlist.v" in window.inputs_summary_label.text()
    window.change_inputs_btn.click()
    assert window.inputs_box.isVisibleTo(window)
    assert "Hide inputs" in window.change_inputs_btn.text()
    window.change_inputs_btn.click()
    assert not window.inputs_box.isVisibleTo(window)


def test_clear_brings_the_inputs_and_the_welcome_back(window, demo_report):
    _show(window, demo_report)
    window.on_clear()
    assert window.inputs_box.isVisibleTo(window)
    assert not window.inputs_summary.isVisibleTo(window)
    assert not window.dashboard.isVisibleTo(window)
    assert "getting started" in window.summary_view.toPlainText()


def test_the_welcome_page_explains_the_first_steps(window):
    text = window.summary_view.toPlainText()
    assert "getting started" in text and "Try the demo data" in text


def test_the_demo_fills_in_the_inputs(window):
    demo = window.load_demo_inputs()
    if demo is None:
        pytest.skip("demo data not present")
    assert window.netlist_picker.path() == demo[0]
    assert window.faults_picker.path() == demo[1]
    assert window.constraints_picker.path() == demo[2]


def test_the_dashboard_summarises_and_links(window, demo_report):
    _show(window, demo_report)
    assert window.dashboard.isVisibleTo(window)
    html = window.dashboard.text()
    assert "Test coverage" in html and "Where to start" in html
    assert "href='tab:triage'" in html and "href='cat:" in html
    top = demo_report.selected_categories[0].subclass_id
    assert f"cat:{top}" in html or "cat:" in html


def test_a_category_link_opens_its_triage_row(window, demo_report):
    _show(window, demo_report)
    table = window.triage_panel.category_table
    target = table.item(1, 0).text()
    window._on_dashboard_link(f"cat:{target}")
    assert window.tabs.currentWidget() is window.triage_panel
    assert table.currentRow() == 1


def test_the_warnings_link_opens_the_hidden_logs_tab(window, demo_report):
    _show(window, demo_report)
    window._on_dashboard_link("tab:logs")
    assert window.tabs.currentWidget() is window._logs_tab


def test_fault_paths_are_elided_in_the_middle_with_a_tooltip(window,
                                                             demo_report):
    _show(window, demo_report)
    assert window.table.textElideMode() == Qt.ElideMiddle
    item = window.table.item(0, 0)
    assert item.toolTip() == item.text()
    for col in range(window.table.columnCount()):
        assert window.table.horizontalHeaderItem(col).toolTip()


def test_triage_columns_are_explained(qapp):
    panel = TriagePanel()
    for col in range(panel.category_table.columnCount()):
        assert panel.category_table.horizontalHeaderItem(col).toolTip()


def test_a_new_report_opens_on_the_summary(window, demo_report):
    window._switch_to_tab("Coverage Loss Table")
    _show(window, demo_report)
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Summary"


# ---------------------------------------------------------------------------
# AI Debug Agent tab
# ---------------------------------------------------------------------------
@pytest.fixture
def panel(qapp, monkeypatch):
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    p = AgentPanel()
    yield p
    p.shutdown()


def test_one_mode_selector_drives_both_switches(panel):
    assert panel.mode_combo.currentData() == "agentic"
    assert panel.agentic_check.isChecked() and panel.cli_mcp_check.isChecked()
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("quick"))
    assert not panel.agentic_check.isChecked()
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("agentic"))
    assert panel.agentic_check.isChecked() and panel.cli_mcp_check.isChecked()


def test_the_old_checkboxes_are_not_on_screen(panel):
    assert not panel.agentic_check.isVisibleTo(panel)
    assert not panel.cli_mcp_check.isVisibleTo(panel)


def test_the_expert_panes_start_hidden_and_toggle(panel):
    prompt_box, trace_box = panel._splitter_boxes[:2]
    assert not prompt_box.isVisibleTo(panel)
    assert not trace_box.isVisibleTo(panel)
    panel.details_btn.setChecked(True)
    assert prompt_box.isVisibleTo(panel) and trace_box.isVisibleTo(panel)
    assert "Hide" in panel.details_btn.text()


def test_verify_reveals_the_trace_it_writes_into(panel):
    panel._last_response = "nothing to see"
    panel.on_verify()
    assert panel._splitter_boxes[1].isVisibleTo(panel)


def test_the_connection_box_folds_into_one_line(panel):
    assert panel._cfg_box.isVisibleTo(panel)
    panel.set_connection_collapsed(True)
    assert not panel._cfg_box.isVisibleTo(panel)
    assert panel.connection_summary.isVisibleTo(panel)
    assert "Connection:" in panel.connection_label.text()
    panel.edit_connection_btn.click()
    assert panel._cfg_box.isVisibleTo(panel)


def test_a_successful_run_folds_the_connection_box(panel):
    panel._start_busy("Calling the LLM")
    panel._on_finished("answer")
    assert not panel._cfg_box.isVisibleTo(panel)


def test_the_layout_choices_round_trip_through_settings(panel, qapp,
                                                        monkeypatch):
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("quick"))
    panel.set_connection_collapsed(True)
    panel.details_btn.setChecked(True)
    saved = panel.export_settings()
    fresh = AgentPanel()
    try:
        fresh.import_settings(saved)
        assert fresh.mode_combo.currentData() == "quick"
        assert not fresh.agentic_check.isChecked()
        assert not fresh._cfg_box.isVisibleTo(fresh)
        assert fresh._splitter_boxes[0].isVisibleTo(fresh)
    finally:
        fresh.shutdown()


def test_the_rarely_used_actions_live_in_the_more_menu(panel):
    titles = [a.text() for a in panel.agent_more_btn.menu().actions()
              if not a.isSeparator()]
    for name in ("Build Prompt Only", "Copy Prompt", "Save Prompt…",
                 "Copy Response", "Save Response…"):
        assert name in titles
    assert any(t.startswith("Suggest Fixes") for t in titles)


# ---------------------------------------------------------------------------
# Help tells a new user how to get everything back
# ---------------------------------------------------------------------------
def test_help_explains_how_to_reopen_everything():
    for phrase in ("Finding your way around", "Change inputs",
                   "+ Analyze several partitions", "Export &#9662;",
                   "More &#9662;", "Show Advanced Tabs", "Open Logs / Warnings",
                   "Go to Tab", "Ctrl+1", "Edit connection",
                   "Show prompt &amp; tool trace", "&#8943; More",
                   "Try the demo data", "Dock back", "Deep investigation",
                   "Quick summary"):
        assert phrase in mw._HELP_HTML, phrase
