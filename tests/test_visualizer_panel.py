"""Offscreen tests for the Tessent Visualizer panel."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QScrollArea

from atpg_coverage_debug_agent.gui.visualizer_panel import VisualizerPanel

PROFILE_DATA = {
    "name": "demo",
    "display_name": "Demo Project",
    "psetup": {"executable": "/bin/echo", "proj": "demoproj/REL1",
               "cfg": "demo.cth"},
    "environment": {"DEMO_LICENSE_SERVER": "1717@licsrv.example.com"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "context": "set_context pattern -scan",
        "load": [
            {"key": "icl", "command": "read_icl", "label": "ICL file",
             "run_dir_glob": "icl/*.icl"},
            {"key": "faults", "command": "read_faults", "label": "Fault list",
             "switches": ["-retain"], "run_dir_glob": "faultlist/*.gz"},
        ],
        "open": "open_visualizer",
        "fault_inspect": ["analyze_fault {fault} -stuck_at {stuck}"],
    },
}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def panel(qapp, tmp_path, monkeypatch):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "demo.json").write_text(json.dumps(PROFILE_DATA),
                                         encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(directory))
    widget = VisualizerPanel()
    widget.reload_profiles()
    return widget


@pytest.fixture()
def files(tmp_path):
    made = {}
    for key in ("icl", "faults"):
        path = tmp_path / f"{key}.dat"
        path.write_text("x", encoding="utf-8")
        made[key] = str(path)
    return made


# ---------------------------------------------------------------------------
# Form construction
# ---------------------------------------------------------------------------
def test_the_form_is_built_from_the_profile(panel):
    assert sorted(panel._path_rows) == ["faults", "icl"]
    assert panel.proj_edit.text() == "demoproj/REL1"
    assert panel.cfg_edit.text() == "demo.cth"


def test_switching_profile_rebuilds_the_form(panel, tmp_path):
    other = dict(PROFILE_DATA, name="other", display_name="Other")
    other["commands"] = dict(PROFILE_DATA["commands"])
    other["commands"]["load"] = [
        {"key": "netlist", "command": "read_netlist", "label": "Netlist"}]
    directory = tmp_path / "profiles"
    (directory / "other.json").write_text(json.dumps(other), encoding="utf-8")
    panel.reload_profiles()
    panel.select_profile("other")
    assert sorted(panel._path_rows) == ["netlist"]


def test_the_analysis_fault_list_is_adopted_and_locked(panel, files):
    panel.set_analysis_faults(files["faults"])
    row = panel._path_rows["faults"]
    assert row.path() == files["faults"]
    assert not row.isEnabled(), "the linked row must not be editable"


def test_unlinking_the_fault_list_re_enables_the_row(panel, files):
    panel.set_analysis_faults(files["faults"])
    panel.use_analysis_faults.setChecked(False)
    assert panel._path_rows["faults"].isEnabled()


def test_the_run_directory_field_only_shows_in_that_mode(panel):
    # isVisibleTo, not isVisible: the panel itself is never shown here.
    assert not panel.run_dir_widget.isVisibleTo(panel)
    panel.mode_run_dir.setChecked(True)
    assert panel.run_dir_widget.isVisibleTo(panel)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def test_the_preview_shows_the_commands_once_the_paths_are_set(panel, files):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    text = panel.commands_view.toPlainText()
    assert "set_context pattern -scan" in text
    assert "open_visualizer" in text
    assert "-retain" in text


def test_the_preview_explains_itself_when_something_is_missing(panel):
    assert "Cannot build the commands yet" in panel.commands_view.toPlainText()


def test_editing_the_preview_is_noticed(panel, files):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    assert not panel._dofile_edited
    panel.commands_view.setPlainText("report_faults -summary")
    assert panel._dofile_edited


def test_regenerating_discards_the_edit(panel, files):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    panel.commands_view.setPlainText("nonsense")
    panel.refresh_preview()
    assert not panel._dofile_edited
    assert "open_visualizer" in panel.commands_view.toPlainText()


# ---------------------------------------------------------------------------
# Fault inspection
# ---------------------------------------------------------------------------
def test_a_fault_adds_its_inspection_commands(panel, files):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    assert panel.add_fault_commands("/top/u_a/Q", "1")
    assert "analyze_fault {/top/u_a/Q} -stuck_at 1" in \
        panel.commands_view.toPlainText()


def test_the_same_fault_is_not_added_twice(panel, files):
    panel.add_fault_commands("/top/u_a/Q", "1")
    panel.add_fault_commands("/top/u_a/Q", "1")
    assert len(panel._extra_commands) == 1


def test_a_hostile_fault_path_is_refused_with_a_message(panel):
    assert not panel.add_fault_commands("/top/{evil}", "1")
    assert "cannot be quoted safely" in panel.status_label.text()


def test_clearing_removes_the_added_commands(panel, files):
    panel.add_fault_commands("/top/u_a/Q", "1")
    panel.clear_extra_commands()
    assert panel._extra_commands == []


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------
def test_launching_with_an_incomplete_form_is_refused_by_name(panel):
    panel.on_launch()
    assert "Still needed" in panel.status_label.text()
    assert "ICL file" in panel.status_label.text()


def test_launching_without_a_terminal_points_at_copy_commands(panel, files,
                                                              monkeypatch):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    monkeypatch.setattr(
        "atpg_coverage_debug_agent.gui.visualizer_panel.find_terminal",
        lambda _pref: None)
    panel.on_launch()
    assert "Copy commands" in panel.status_label.text()


def test_the_copied_chain_covers_every_stage(panel, files, qapp):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    panel.on_copy_commands()
    text = QApplication.clipboard().text()
    assert "-proj demoproj/REL1" in text
    assert "setenv DEMO_LICENSE_SERVER" in text
    assert "open_visualizer" in text


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_the_form_round_trips_through_settings(panel, files):
    for key, path in files.items():
        panel._path_rows[key].set_path(path)
    panel.licence_edit.setText("2020@other.example.com")
    saved = panel.export_settings()
    panel.import_settings(saved)
    assert panel.export_settings() == saved


def test_import_ignores_a_value_that_is_not_a_mapping(panel):
    panel.import_settings(None)  # must not raise


# ---------------------------------------------------------------------------
# The tab in the main window
# ---------------------------------------------------------------------------
def test_the_tab_is_present_and_scrolled(qapp):
    """A tall panel placed directly in the tabs freezes the window minimum."""
    from atpg_coverage_debug_agent.gui.main_window import MainWindow

    window = MainWindow()
    try:
        titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert "Tessent Visualizer" in titles
        holder = window.tabs.widget(titles.index("Tessent Visualizer"))
        assert isinstance(holder, QScrollArea)
        assert window.minimumSizeHint().height() < 800
    finally:
        window.close()
