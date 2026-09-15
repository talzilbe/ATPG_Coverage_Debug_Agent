"""Saved launch configurations, and per-user persistence of the setup fields.

Opening the viewer needs a project, a licence server, a workarea and a set of
design paths. Retyping those every session is the friction this removes: the
fields survive a restart, and a named configuration recalls a whole setup at
once.

Offscreen, and driven through the panel API rather than its dialogs -- a real
``QInputDialog.exec`` is a C++ slot that cannot be monkeypatched and would hang
the run.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.config.settings import AppSettings
from atpg_coverage_debug_agent.gui.visualizer_panel import (
    _UNSAVED_LABEL,
    VisualizerPanel,
)

PROFILE_DATA = {
    "name": "demo",
    "display_name": "Demo Project",
    "psetup": {"executable": "/bin/echo", "proj": "demoproj/REL1",
               "cfg": "demo.cth"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "context": "set_context pattern -scan",
        "load": [
            {"key": "icl", "command": "read_icl", "label": "ICL file"},
            {"key": "faults", "command": "read_faults", "label": "Fault list"},
        ],
        "open": "open_visualizer",
    },
}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def profiles_dir(tmp_path, monkeypatch):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "demo.json").write_text(json.dumps(PROFILE_DATA),
                                         encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(directory))
    return directory


@pytest.fixture()
def panel(qapp, profiles_dir):
    widget = VisualizerPanel()
    widget.reload_profiles()
    return widget


def _fill(panel, *, licence="1717@licsrv.example.com", ward="/tmp/ward",
          proj="myproj/REL2", cfg="my.cth"):
    panel.licence_edit.setText(licence)
    panel.ward_row.set_path(ward)
    panel.proj_edit.setText(proj)
    panel.cfg_edit.setText(cfg)
    panel._path_rows["icl"].set_path("/designs/a.icl")
    panel._path_rows["faults"].set_path("/designs/a.faults.gz")


# ---------------------------------------------------------------------------
# The setup fields survive a restart
# ---------------------------------------------------------------------------
def test_the_licence_server_and_workarea_round_trip(panel, profiles_dir, qapp):
    """These two are pure retyping cost; they must come back on restart."""
    _fill(panel)
    saved = panel.export_settings()
    assert saved["licence_server"] == "1717@licsrv.example.com"
    assert saved["ward"] == "/tmp/ward"

    fresh = VisualizerPanel()
    fresh.reload_profiles()
    fresh.import_settings(saved)
    assert fresh.licence_edit.text() == "1717@licsrv.example.com"
    assert fresh.ward_row.path() == "/tmp/ward"
    assert fresh.proj_edit.text() == "myproj/REL2"
    assert fresh.cfg_edit.text() == "my.cth"
    assert fresh._path_rows["icl"].path() == "/designs/a.icl"


def test_the_settings_file_is_per_user(tmp_path, monkeypatch):
    """Settings live under the user's home, so two users never collide."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from importlib import reload

    from atpg_coverage_debug_agent.config import settings as settings_module
    reload(settings_module)
    assert str(tmp_path) in str(settings_module._DEFAULT_CONFIG_FILE)
    reload(settings_module)


def test_settings_persist_the_visualizer_blob_to_disk(tmp_path, panel):
    _fill(panel)
    path = tmp_path / "settings.json"
    s = AppSettings()
    s.visualizer = panel.export_settings()
    s.save(path)

    reloaded = AppSettings.load(path)
    assert reloaded.visualizer["licence_server"] == "1717@licsrv.example.com"
    assert reloaded.visualizer["ward"] == "/tmp/ward"


# ---------------------------------------------------------------------------
# Named configurations
# ---------------------------------------------------------------------------
def test_a_configuration_can_be_saved_and_recalled(panel):
    _fill(panel)
    assert panel.save_preset("nightly run")
    assert panel.preset_names() == ["nightly run"]

    # Wipe the form, then bring the whole setup back in one step.
    _fill(panel, licence="", ward="", proj="", cfg="")
    assert panel.licence_edit.text() == ""

    assert panel.load_preset("nightly run")
    assert panel.licence_edit.text() == "1717@licsrv.example.com"
    assert panel.ward_row.path() == "/tmp/ward"
    assert panel.proj_edit.text() == "myproj/REL2"
    assert panel._path_rows["faults"].path() == "/designs/a.faults.gz"


def test_saving_under_an_existing_name_replaces_it(panel):
    _fill(panel, ward="/tmp/first")
    panel.save_preset("shared")
    _fill(panel, ward="/tmp/second")
    panel.save_preset("shared")
    assert panel.preset_names() == ["shared"]
    panel.load_preset("shared")
    assert panel.ward_row.path() == "/tmp/second"


def test_an_unnamed_configuration_is_refused(panel):
    _fill(panel)
    assert not panel.save_preset("   ")
    assert panel.preset_names() == []


def test_a_configuration_can_be_deleted(panel):
    _fill(panel)
    panel.save_preset("temporary")
    assert panel.delete_preset("temporary")
    assert panel.preset_names() == []
    assert not panel.delete_preset("temporary")


def test_loading_an_unknown_configuration_reports_rather_than_raises(panel):
    assert not panel.load_preset("never saved")


def test_configurations_survive_a_restart(panel, profiles_dir):
    _fill(panel, ward="/tmp/ward_a")
    panel.save_preset("alpha")
    _fill(panel, ward="/tmp/ward_b")
    panel.save_preset("beta")
    saved = panel.export_settings()

    fresh = VisualizerPanel()
    fresh.reload_profiles()
    fresh.import_settings(saved)
    assert fresh.preset_names() == ["alpha", "beta"]
    fresh.load_preset("alpha")
    assert fresh.ward_row.path() == "/tmp/ward_a"


def test_the_remembered_configuration_is_selected_but_not_reapplied(panel,
                                                                    profiles_dir):
    """The form already holds the last session; re-applying would undo edits."""
    _fill(panel, ward="/tmp/saved_ward")
    panel.save_preset("alpha")
    # An edit made after saving the configuration, which must survive restart.
    panel.ward_row.set_path("/tmp/edited_after")
    saved = panel.export_settings()
    assert saved["last_preset"] == "alpha"

    fresh = VisualizerPanel()
    fresh.reload_profiles()
    fresh.import_settings(saved)
    assert fresh.current_preset_name() == "alpha"
    assert fresh.ward_row.path() == "/tmp/edited_after"


def test_a_saved_configuration_does_not_nest_the_configuration_store(panel):
    """A preset holding the preset store would grow without bound."""
    _fill(panel)
    panel.save_preset("alpha")
    panel.save_preset("beta")
    for cfg in panel.export_settings()["presets"].values():
        assert "presets" not in cfg
        assert "last_preset" not in cfg


# ---------------------------------------------------------------------------
# The combo reflects the store
# ---------------------------------------------------------------------------
def test_the_combo_offers_every_saved_configuration(panel):
    _fill(panel)
    panel.save_preset("zulu")
    panel.save_preset("alpha")
    labels = [panel.preset_combo.itemText(i)
              for i in range(panel.preset_combo.count())]
    assert labels == [_UNSAVED_LABEL, "alpha", "zulu"]


def test_delete_is_disabled_until_a_configuration_is_selected(panel):
    assert not panel.delete_preset_btn.isEnabled()
    _fill(panel)
    panel.save_preset("alpha")
    assert panel.delete_preset_btn.isEnabled()
    panel.delete_preset("alpha")
    assert not panel.delete_preset_btn.isEnabled()


def test_selecting_a_configuration_in_the_combo_loads_it(panel):
    _fill(panel, ward="/tmp/ward_a")
    panel.save_preset("alpha")
    _fill(panel, ward="/tmp/ward_b")
    panel.save_preset("beta")

    index = panel.preset_combo.findData("alpha")
    panel.preset_combo.setCurrentIndex(index)
    assert panel.ward_row.path() == "/tmp/ward_a"


def test_saving_a_configuration_asks_the_window_to_persist(panel):
    seen = []
    panel.config_changed.connect(lambda: seen.append(1))
    _fill(panel)
    panel.save_preset("alpha")
    assert seen, "saving a configuration must trigger a settings write"


# ---------------------------------------------------------------------------
# Older settings files still load
# ---------------------------------------------------------------------------
def test_a_settings_blob_without_configurations_still_loads(panel):
    panel.import_settings({"proj": "old/REL1", "ward": "/tmp/old",
                           "licence_server": "1717@old.example.com"})
    assert panel.preset_names() == []
    assert panel.proj_edit.text() == "old/REL1"
    assert panel.ward_row.path() == "/tmp/old"


def test_a_corrupt_configuration_entry_is_skipped(panel):
    panel.import_settings({"presets": {"good": {"ward": "/tmp/g"},
                                       "bad": "not a mapping"}})
    assert panel.preset_names() == ["good"]
