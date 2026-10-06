"""The new-user aids in the main window, agent tab and Visualizer tab."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QSplitter  # noqa: E402

from atpg_coverage_debug_agent.app import run_analysis  # noqa: E402
from atpg_coverage_debug_agent.config import settings as settings_mod  # noqa: E402
from atpg_coverage_debug_agent.gui import main_window as mw  # noqa: E402
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel  # noqa: E402
from atpg_coverage_debug_agent.launcher.profiles import ToolProfile  # noqa: E402
from atpg_coverage_debug_agent.launcher.visualizer import (  # noqa: E402
    VisualizerInputs, launch_health, profile_licence,
)

STATS_TEXT = """
   AU (atpg_untestable)                                 60 ( 6.00%)
     TC (tied_cells)                                    40 ( 4.00%)
     test_coverage                                   93.50%
"""


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


@pytest.fixture(scope="module")
def demo_report():
    demo = mw._demo_inputs()
    if demo is None:
        pytest.skip("demo data not present")
    return run_analysis(*demo)


def _show(win, report):
    win._base_report = report
    win._apply_report(report)
    win._after_new_report()


def test_each_input_box_shows_a_verdict(window):
    demo = mw._demo_inputs()
    window.netlist_picker.set_path(demo[0])
    assert "Verilog netlist" in window.netlist_picker.status.text()
    window.faults_picker.set_path("/no/such/file.mtfi")
    assert "not found" in window.faults_picker.status.text()
    assert window.faults_picker.last_check.state == "error"


def test_recent_analyses_refill_every_box(window):
    entry = {"netlist": "/a/n.v", "faults": "/a/f.mtfi",
             "constraints": "/a/c.do"}
    window._settings.remember_inputs(entry)
    window._refresh_recent_menus()
    actions = [a for a in window.recent_menu.actions() if a.isEnabled()]
    assert actions and "n" in actions[0].text()
    assert window.file_recent_menu.actions()
    actions[0].trigger()
    assert window.netlist_picker.path() == "/a/n.v"
    assert window.constraints_picker.path() == "/a/c.do"


def test_tessent_reports_are_not_an_input_box(window):
    assert not hasattr(window, "tool_reports_picker")
    assert len(window.inputs_box.findChildren(mw._FilePicker)) == 4


def test_empty_tabs_offer_next_steps_until_a_report(window, demo_report):
    tab = window._table_tab
    assert window.table_empty.isVisibleTo(tab)
    assert window.triage_panel.empty_state.isVisibleTo(window.triage_panel)
    _show(window, demo_report)
    assert not window.table_empty.isVisibleTo(tab)
    assert window.table_content.isVisibleTo(tab)
    window.on_clear()
    assert window.table_empty.isVisibleTo(tab)


def test_the_tour_walks_the_tabs_and_is_remembered(window, demo_report):
    _show(window, demo_report)
    bar = window.tour_bar
    assert bar.isVisibleTo(window) and bar.step == 0
    for _ in range(len(mw.TOUR_STEPS) - 1):
        bar.next_btn.click()
    assert window.tabs.tabText(window.tabs.currentIndex()) == "AI Debug Agent"
    assert bar.next_btn.text() == "Finish"
    bar.next_btn.click()
    assert not bar.isVisibleTo(window)
    assert window._settings.tour_done
    _show(window, demo_report)
    assert not bar.isVisibleTo(window)


def test_progress_shows_elapsed_and_time_left(window):
    window._reset_progress_clock()
    t0 = window._run_started
    window.progress_text(0, 100, "Analysed 0/100 coverage-loss faults", t0)
    text = window.progress_text(25, 100, "Analysed 25/100 coverage-loss faults",
                                t0 + 10)
    assert "elapsed" in text and "left in this step" in text


def test_cancel_restores_the_window(window):
    window.analyze_btn.setEnabled(False)
    window.cancel_btn.setEnabled(True)
    window._on_cancelled()
    assert window.analyze_btn.isEnabled() and not window.cancel_btn.isEnabled()
    assert "cancelled" in window.statusBar().currentMessage()


def test_the_worker_stops_at_the_next_checkpoint(monkeypatch):
    from atpg_coverage_debug_agent.app import AnalysisCancelled
    from atpg_coverage_debug_agent.gui import workers

    class _Thread:
        @staticmethod
        def currentThread():
            return _Thread()

        def isInterruptionRequested(self):
            return True

    class _Sig:
        def emit(self, *a):
            raise AssertionError("must not emit after Cancel")

    monkeypatch.setattr(workers, "QThread", _Thread)
    with pytest.raises(AnalysisCancelled):
        workers._progress_or_cancel(_Sig())(1, 2, "x")


def test_layout_is_saved_and_restored(window):
    splitters = window._splitters()
    assert splitters
    window.save_layout()
    assert window._settings.window_geometry
    assert set(window._settings.splitter_states) == set(splitters)
    assert window.restore_layout() is True


def test_class_filter_reads_in_plain_words(window):
    texts = [window.class_filter.itemText(i)
             for i in range(window.class_filter.count())]
    assert "AU — ATPG untestable" in texts
    window.class_filter.setCurrentIndex(window.class_filter.findData("AU"))
    assert window.class_filter.currentData() == "AU"


def test_tool_reports_can_be_added_after_analysis(window, demo_report,
                                                  tmp_path):
    _show(window, demo_report)
    stats = tmp_path / "stats.log"
    stats.write_text(STATS_TEXT)
    window.apply_tool_reports(str(stats))
    assert window._report.tool_evidence is not None
    assert "measured by Tessent" in window.dashboard.text()


def test_agent_readiness_points_at_the_first_gap(qapp, monkeypatch):
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    panel = AgentPanel()
    try:
        panel.cli_path_edit.setText("/no/such/copilot")
        items = {label: state for label, state, *_ in panel.readiness_items()}
        assert items["Copilot CLI"] == "fail" and items["Report"] == "fail"
        assert panel._readiness_action == "browse_cli"
        assert "Before you run" in panel.readiness_label.text()
        panel._auth_state = "ok"
        assert dict((l, s) for l, s, *_ in panel.readiness_items())[
            "Sign-in"] == "ok"
        texts = [panel.mode_combo.itemText(i)
                 for i in range(panel.mode_combo.count())]
        assert texts[0].startswith("Deep investigation")
        assert texts[1].startswith("Quick summary")
    finally:
        panel.shutdown()


def test_agent_suggests_the_visualizer_before_a_deep_run(qapp, monkeypatch):
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    panel = AgentPanel()
    try:
        idx = panel.backend_combo.findData("cli")
        panel.backend_combo.setCurrentIndex(idx)
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("agentic"))
        panel.set_tessent_provider(lambda: None)
        items = {l: (s, a) for l, s, _, a in panel.readiness_items()}
        assert items["Tessent session"] == ("optional", "open_visualizer")
        assert panel._should_suggest_visualizer()

        opened = []
        panel.open_visualizer_requested.connect(lambda: opened.append(1))
        panel._readiness_action = "open_visualizer"
        panel._on_readiness_fix()
        assert opened

        panel.set_tessent_provider(
            lambda: {"live": True, "agent_eval": True, "profile": "p"})
        assert dict((l, s) for l, s, *_ in panel.readiness_items())[
            "Tessent session"] == "ok"
        assert not panel._should_suggest_visualizer()

        panel.set_tessent_provider(lambda: None)
        panel._skip_visualizer_hint = True
        assert not panel._should_suggest_visualizer()
        assert panel.export_settings()["skip_visualizer_hint"] is True
    finally:
        panel.shutdown()


# ---------------------------------------------------------------------------
# Visualizer: shared licence default and the pre-launch checks
# ---------------------------------------------------------------------------
_LIC = "1717@a.example.com:1717@b.example.com"


def _profile(tmp_path, psetup_exists=True):
    exe = tmp_path / "psetup"
    if psetup_exists:
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return ToolProfile.from_dict({
        "name": "demo", "display_name": "Demo",
        "psetup": {"executable": str(exe), "proj": "p", "cfg": "c"},
        "environment": {"SALT_LICENSE_SERVER": _LIC},
        "tool": {"executable": str(tmp_path / "tessent")},
        "commands": {"load": [{"key": "icl", "command": "read_icl",
                               "label": "ICL file", "required": True}],
                     "open": "open_visualizer"},
    })


def test_the_profile_licence_is_the_default(tmp_path):
    assert profile_licence(_profile(tmp_path)) == _LIC


def test_launch_health_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    profile = _profile(tmp_path, psetup_exists=False)
    checks = {c.label: c for c in launch_health(profile, VisualizerInputs())}
    assert not checks["Project setup wrapper"].ok
    assert not checks["ICL file"].ok
    assert checks["Licence server"].ok
    assert checks["Tool executable"].blocking is False
    icl = tmp_path / "d.icl"
    icl.write_text("x")
    good = _profile(tmp_path)
    checks = {c.label: c for c in launch_health(
        good, VisualizerInputs(paths={"icl": str(icl)}))}
    assert checks["Project setup wrapper"].ok and checks["ICL file"].ok


def test_the_shipped_site_profile_carries_the_shared_licence():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "profiles", "ttlc.json")
    if not os.path.isfile(path):
        pytest.skip("site profile not present")
    with open(path) as fh:
        env = json.load(fh)["environment"]
    value = next(v for k, v in env.items() if "LICENSE" in k.upper())
    assert value.count("@") == 5


def test_the_panel_prefills_the_licence_and_shows_health(qapp, tmp_path,
                                                         monkeypatch):
    from atpg_coverage_debug_agent.gui import visualizer_panel as vp
    profile = _profile(tmp_path)
    monkeypatch.setattr(vp, "list_profiles", lambda: [profile])
    panel = vp.VisualizerPanel()
    assert panel.licence_edit.text() == _LIC
    panel.licence_edit.setText("")
    panel.import_settings({"profile": "demo", "licence_server": ""})
    assert panel.licence_edit.text() == _LIC
    panel.licence_edit.setText("1717@other.example.com")
    panel.reset_licence()
    assert panel.licence_edit.text() == _LIC
    assert "ICL file" in panel.health_label.text()
