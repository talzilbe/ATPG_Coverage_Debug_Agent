"""Theme, shortcuts, palette, checklist, drag & drop, undo and other UX aids."""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from atpg_coverage_debug_agent.agent import answer_format  # noqa: E402
from atpg_coverage_debug_agent.app import run_analysis  # noqa: E402
from atpg_coverage_debug_agent.config import settings as settings_mod  # noqa: E402
from atpg_coverage_debug_agent.gui import main_window as mw  # noqa: E402
from atpg_coverage_debug_agent.gui import shortcuts, theme  # noqa: E402
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel  # noqa: E402
from atpg_coverage_debug_agent.gui.command_palette import (  # noqa: E402
    PaletteEntry, rank_entries,
)
from atpg_coverage_debug_agent.gui.getting_started import STEP_IDS  # noqa: E402
from atpg_coverage_debug_agent.gui.input_check import classify_file  # noqa: E402
from atpg_coverage_debug_agent.gui.preferences import PreferencesDialog  # noqa: E402
from atpg_coverage_debug_agent.reporting.html_report import build_html_report  # noqa: E402
from atpg_coverage_debug_agent.reporting.session_report import save_report  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _light_afterwards(qapp):
    yield
    theme.apply_app(qapp, theme.LIGHT, 0)


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "_DEFAULT_CONFIG_FILE",
                        tmp_path / "settings.json")
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    win = mw.MainWindow()
    yield win
    win.agent_panel.shutdown()
    win.close()


@pytest.fixture(scope="module")
def demo():
    demo = mw._demo_inputs()
    if demo is None:
        pytest.skip("demo data not present")
    return demo


@pytest.fixture(scope="module")
def demo_report(demo):
    return run_analysis(*demo)


def _show(win, report):
    win._base_report = report
    win._apply_report(report)
    win._after_new_report()


def _declared_colours(html):
    """(role, colour) for every colour in a CSS declaration of *html*."""
    out = []
    for m in theme._DECL_RE.finditer(html):
        role = theme._role_for(m.group(1))
        for c in theme._COLOUR_RE.findall(m.group(3)):
            out.append((role, c))
    return out


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
def test_light_theme_leaves_every_colour_alone():
    theme.set_current(theme.LIGHT)
    assert theme.adapt(mw._HELP_HTML) == mw._HELP_HTML


def test_dark_theme_has_dark_fills_and_light_text(demo_report):
    theme.set_current(theme.DARK)
    answer = answer_format.render_answer_html(
        answer_format.parse_answer("## Verdict\nAU.TC is tied off.\n"
                                   "## Actions\n1. Waive it\n"),
        set(), lambda s: s)
    sources = [mw._HELP_HTML, mw._WELCOME_HTML, answer,
               build_html_report(demo_report)]
    for html in sources:
        adapted = theme.adapt(html)
        colours = _declared_colours(adapted)
        assert colours
        for role, colour in colours:
            lum = theme.luminance(colour)
            if role == "bg":
                assert lum < 0.5, colour
            elif role == "fg":
                assert lum >= 0.6, colour
        # A light background must never survive into the dark theme.
        assert not re.search(r"background(-color)?\s*:\s*#f", adapted, re.I)


def test_character_entities_are_not_mistaken_for_colours():
    theme.set_current(theme.DARK)
    html = "<p style='color:#333'>Next:&#160;go &#9654; on</p>"
    assert "&#160;" in theme.adapt(html) and "&#9654;" in theme.adapt(html)


def test_exported_html_does_not_depend_on_the_theme(demo_report):
    theme.set_current(theme.LIGHT)
    light = build_html_report(demo_report)
    theme.set_current(theme.DARK)
    assert build_html_report(demo_report) == light


def test_switching_theme_restyles_the_views_and_is_remembered(window,
                                                              demo_report):
    _show(window, demo_report)
    light_html = window.summary_view.toHtml()
    window.set_theme(theme.DARK)
    assert window._settings.theme == "dark"
    assert theme.is_dark()
    assert window.summary_view.toHtml() != light_html
    assert window.theme_actions["dark"].isChecked()
    assert "Light" in window.theme_btn.text()
    window.toggle_theme()
    assert window._settings.theme == "light"
    assert window.summary_view.toHtml() == light_html


def test_static_stylesheets_follow_the_theme(window):
    raw = window.analyze_btn.styleSheet()
    window.set_theme(theme.DARK)
    assert window.analyze_btn.styleSheet() != raw
    window.set_theme(theme.LIGHT)
    assert window.analyze_btn.styleSheet() == raw


def test_font_size_is_clamped_and_saved(window):
    assert window.set_font_delta(50) == theme.MAX_FONT_DELTA
    assert window._settings.font_delta == theme.MAX_FONT_DELTA
    assert window.change_font(-100) == theme.MIN_FONT_DELTA
    assert window.set_font_delta(0) == 0


# ---------------------------------------------------------------------------
# Shortcuts and help
# ---------------------------------------------------------------------------
def _window_keys(win):
    from PySide6.QtGui import QAction, QKeySequence
    keys = []
    for act in win.findChildren(QAction):
        for seq in act.shortcuts():
            if not seq.isEmpty():
                keys.append(seq.toString(QKeySequence.PortableText))
    return keys


def test_every_window_shortcut_is_bound_once(window):
    from PySide6.QtGui import QKeySequence
    keys = _window_keys(window)
    assert len(keys) == len(set(keys)), sorted(keys)
    for s in shortcuts.window_shortcuts():
        expected = QKeySequence(s.keys).toString(QKeySequence.PortableText)
        assert expected in keys, s


def test_help_maps_every_shortcut():
    assert '<a name="shortcuts"></a>' in mw._HELP_HTML
    for s in shortcuts.SHORTCUTS:
        assert f"<code>{shortcuts.html.escape(s.keys)}</code>" in mw._HELP_HTML


def test_every_help_link_has_its_anchor(window):
    from PySide6.QtWidgets import QToolButton
    anchors = set(mw._TAB_HELP.values())
    for btn in window.findChildren(QToolButton):
        if btn.property("help_anchor"):
            anchors.add(btn.property("help_anchor"))
    assert {"inputs", "actions"} <= anchors
    for name in anchors:
        assert f'<a name="{name}"></a>' in mw._HELP_HTML, name


# ---------------------------------------------------------------------------
# Drag and drop
# ---------------------------------------------------------------------------
def test_dropped_files_are_recognised_by_content(demo, demo_report, tmp_path):
    netlist, faults, constraints = demo
    assert classify_file(netlist) == "netlist"
    assert classify_file(faults) == "faults"
    assert classify_file(constraints) == "constraints"
    assert classify_file(str(tmp_path)) == "outdir"
    report = tmp_path / "r.json"
    save_report(demo_report, str(report), dump_categories=False)
    assert classify_file(str(report)) == "report"
    junk = tmp_path / "notes.txt"
    junk.write_text("hello\n")
    assert classify_file(str(junk)) is None


def test_dropping_files_fills_the_boxes(window, demo, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("hello\n")
    result = window.handle_dropped_paths(list(demo) + [str(junk)])
    assert window.netlist_picker.path() == demo[0]
    assert window.faults_picker.path() == demo[1]
    assert window.constraints_picker.path() == demo[2]
    assert result["unknown"] == [str(junk)]
    assert "not recognised" in window.toast.label.text()


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------
def test_a_waiver_can_be_undone(window, demo_report):
    _show(window, demo_report)
    before = window._report.summary.coverage_loss_count
    window.table.selectRow(0)
    window._exclude_selected_faults()
    assert window._report.summary.coverage_loss_count == before - 1
    assert window.undo_action.isEnabled()
    assert window.toast.action_btn.text() == "Undo"
    assert window.undo_exclusion()
    assert window._report.summary.coverage_loss_count == before
    assert not window.undo_action.isEnabled()
    assert not window.undo_exclusion()


def test_a_new_report_forgets_the_undo_history(window, demo_report):
    _show(window, demo_report)
    window.table.selectRow(0)
    window._exclude_selected_faults()
    window.on_clear()
    assert window._undo_stack == [] and not window.undo_action.isEnabled()


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------
def test_ranking_prefers_contiguous_matches():
    hits = []
    entries = [PaletteEntry(label, lambda lbl=label: hits.append(lbl))
               for label in ("File \u203a Export CSV\u2026",
                             "View \u203a Theme \u203a Dark",
                             "Edit \u203a Preferences\u2026")]
    assert rank_entries("dark", entries)[0].label.endswith("Dark")
    assert rank_entries("ecsv", entries)[0].label.startswith("File")
    assert rank_entries("zzz", entries) == []
    assert len(rank_entries("", entries)) == 3


def test_palette_lists_menu_commands_and_faults(window, demo_report):
    labels = [e.label for e in window.palette_entries("theme dark")]
    assert labels and labels[0] == "View \u203a Theme \u203a Dark"
    assert any("Preferences" in e.label for e in window.palette_entries("pref"))
    assert "run Analyze" in window.palette_entries("@x")[0].label
    _show(window, demo_report)
    fault = demo_report.fault_results[0].fault.fault_object
    entries = window.palette_entries("@" + fault[-12:])
    assert any(e.label == fault for e in entries)
    next(e for e in entries if e.label == fault).callback()
    assert window.tabs.tabText(window.tabs.currentIndex()) == \
        "Coverage Loss Table"


def test_palette_runs_the_chosen_command(window):
    palette = window.command_palette
    palette.open()
    palette.edit.setText("toggle light")
    assert palette.entries()
    palette.run_current()
    assert theme.is_dark()
    assert not palette.isVisible()


# ---------------------------------------------------------------------------
# Getting Started, preferences, status bar, find, toasts
# ---------------------------------------------------------------------------
def test_getting_started_ticks_itself_and_is_remembered(window, demo,
                                                        demo_report):
    gs = window.getting_started
    assert gs.isVisibleTo(window._summary_tab) and gs.done_steps() == []
    window.netlist_picker.set_path(demo[0])
    window.faults_picker.set_path(demo[1])
    assert gs.is_done("inputs")
    _show(window, demo_report)
    assert gs.is_done("analyze")
    window._switch_to_tab("Triage & Fix Plan")
    assert gs.is_done("triage")
    window.agent_panel.run_completed.emit()
    window.visualizer_panel.launched.emit()
    assert gs.all_done()
    assert window._settings.getting_started_done == STEP_IDS
    window.set_checklist_visible(False)
    assert window._settings.getting_started_hidden
    assert not gs.isVisibleTo(window._summary_tab)


def test_preferences_apply_every_choice(window, qapp):
    dlg = PreferencesDialog(window.preference_values(), window)
    dlg.theme_combo.setCurrentIndex(dlg.theme_combo.findData(theme.DARK))
    dlg.font_spin.setValue(2)
    dlg.advanced_check.setChecked(True)
    dlg.checklist_check.setChecked(False)
    window.apply_preferences(dlg.values())
    assert theme.is_dark() and theme.font_delta() == 2
    assert window.advanced_tabs_action.isChecked()
    assert window._settings.getting_started_hidden
    window.set_font_delta(0)


def test_status_bar_shows_report_agent_and_visualizer(window, demo_report):
    assert window.report_chip.text() == "No report loaded"
    assert "Visualizer" in window.vis_chip.text()
    assert window.agent_chip.text()
    _show(window, demo_report)
    assert f"{demo_report.summary.coverage_loss_count:,} loss" in \
        window.report_chip.text()


def test_find_targets_the_current_tab(window, demo_report):
    _show(window, demo_report)
    window._switch_to_tab("Summary")
    window.on_find()
    assert window.find_bar.isVisibleTo(window)
    assert window.find_bar._target is window.summary_view
    window.find_bar.edit.setText("Coverage")
    assert window.find_bar.find_next()
    window._switch_to_tab("Coverage Loss Table")
    assert not window.find_bar.isVisibleTo(window)


def test_toast_runs_its_action_once(window):
    hits = []
    window.notify("done", "ok", action="Undo", callback=lambda: hits.append(1))
    assert window.toast.isVisibleTo(window)
    window.toast.action_btn.click()
    window.toast.action_btn.click()
    assert hits == [1]
    assert not window.toast.isVisibleTo(window)


def test_missing_constraints_is_a_notice_not_a_dialog(window, demo):
    window.netlist_picker.set_path(demo[0])
    window.faults_picker.set_path(demo[1])
    window.constraints_picker.set_path("")
    window._start_analysis = lambda inputs: None
    window.on_analyze()
    assert "No constraint file" in window.toast.label.text()


def test_dark_check_boxes_get_a_drawn_tick(qapp):
    theme.apply_app(qapp, theme.DARK)
    sheet = qapp.styleSheet()
    match = re.search(r"QCheckBox::indicator:checked \{ image: url\(([^)]+)\)",
                      sheet)
    assert match and os.path.isfile(match.group(1))
    assert "background-color: #4fc1ff" not in sheet


def _live_panel(port_file):
    from types import SimpleNamespace
    from atpg_coverage_debug_agent.gui.visualizer_panel import VisualizerPanel
    panel = VisualizerPanel()
    panel._begin_session(SimpleNamespace(token="tok", port_file=str(port_file)))
    panel._poll_session()
    return panel


def test_a_closed_tessent_session_is_noticed(qapp, tmp_path):
    import socket
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port_file = tmp_path / "control.port"
    port_file.write_text(str(server.getsockname()[1]), encoding="utf-8")
    panel = _live_panel(port_file)
    try:
        assert panel.has_live_session()
        assert panel._check_alive() and panel.has_live_session()
        server.close()
        panel._check_alive()
        assert panel.has_live_session(), "one miss is not enough"
        assert not panel._check_alive()
        assert not panel.has_live_session()
        assert panel.agent_target() is None
        assert "closed" in panel.session_label.text()
    finally:
        server.close()
        panel.stop_log_tail()


# ---------------------------------------------------------------------------
# Start-up sign-in check
# ---------------------------------------------------------------------------
def _fake_cli(tmp_path, exit_code):
    import stat
    import sys
    script = tmp_path / f"copilot_{exit_code}"
    script.write_text(f"#!{sys.executable}\nimport sys\nprint('pong')\n"
                      f"sys.exit({exit_code})\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _wait_for_auth(qapp, panel, seconds=20):
    import time
    deadline = time.monotonic() + seconds
    while panel._auth_proc is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert panel._auth_proc is None, "the sign-in check never finished"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_saved_token_is_checked_at_start_up(window, qapp, tmp_path,
                                              monkeypatch, exit_code):
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    panel = window.agent_panel
    panel.auth_remember_check.setChecked(False)
    panel.cli_path_edit.setText(_fake_cli(tmp_path, exit_code))
    panel.auth_token_edit.setText("github_pat_test")
    seen = []
    panel.auto_auth_finished.connect(lambda ok, msg: seen.append((ok, msg)))
    assert window.auto_check_sign_in()
    assert "checking" in dict((l, d) for l, _s, d, _a in
                              panel.readiness_items())["Sign-in"]
    _wait_for_auth(qapp, panel)
    assert seen and seen[0][0] is (exit_code == 0)
    assert panel._auth_state == ("ok" if exit_code == 0 else "fail")
    if exit_code == 0:
        assert "ready" in window.toast.label.text()
        assert "sign-in" not in window.agent_chip.text()
    else:
        assert "failed" in window.toast.label.text()
        window.toast.action_btn.click()
        assert window.agent_panel.tabs.currentIndex() == \
            window.agent_panel.tabs.count() - 1


def test_no_token_means_no_start_up_check(window, monkeypatch, tmp_path):
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    window.agent_panel.auth_token_edit.setText("")
    window.agent_panel.cli_path_edit.setText(_fake_cli(tmp_path, 0))
    assert not window.auto_check_sign_in()
    assert window.agent_panel._auth_proc is None
