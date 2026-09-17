"""Profiles and launch configurations as files: import, save, load.

Dialogs are never executed. The QFileDialog static pickers are replaced with
plain callables; every other path goes through the file-taking methods.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.launcher import profiles as prof
from atpg_coverage_debug_agent.launcher.visualizer import (
    CONFIG_KIND,
    ConfigFileError,
    config_file_stem,
    default_config_dir,
    read_launch_config,
    write_launch_config,
)
from atpg_coverage_debug_agent.gui import visualizer_panel as vp_mod
from atpg_coverage_debug_agent.gui.visualizer_panel import VisualizerPanel

REPO = Path(__file__).resolve().parent.parent

PROFILE = {
    "name": "demo",
    "display_name": "Demo Project",
    "psetup": {"executable": "/bin/echo", "proj": "demoproj/REL1",
               "cfg": "demo.cth"},
    "environment": {"DEMO_LICENSE_SERVER": "1717@licsrv.example.com"},
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
def dirs(tmp_path, monkeypatch):
    env_dir = tmp_path / "env_profiles"
    env_dir.mkdir()
    (env_dir / "demo.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(env_dir))
    user_dir = tmp_path / "user_profiles"
    monkeypatch.setenv("ATPG_USER_PROFILE_DIR", str(user_dir))
    monkeypatch.setenv("ATPG_VIS_CONFIG_DIR", str(tmp_path / "configs"))
    return {"env": env_dir, "user": user_dir, "tmp": tmp_path}


@pytest.fixture()
def panel(qapp, dirs):
    widget = VisualizerPanel()
    widget.reload_profiles()
    yield widget
    widget.stop_log_tail()


def _write_profile(path: Path, **changes) -> Path:
    data = dict(PROFILE, **changes)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# profiles.py
# ---------------------------------------------------------------------------

def test_user_profile_dir_sits_between_env_and_repo(dirs):
    path = prof.profile_search_path()
    assert path[0] == dirs["env"]
    assert path[1] == dirs["user"]
    assert path[-1].name == "profiles" and path[-1].parent == REPO


def test_import_copies_the_file_under_the_profile_name(dirs):
    src = _write_profile(dirs["tmp"] / "anything.json", name="other",
                         display_name="Other")
    profile, target, warnings = prof.import_profile_file(str(src))
    assert profile.name == "other"
    assert target == dirs["user"] / "other.json"
    assert target.is_file()
    assert warnings == []
    assert "other" in prof.load_profiles()


def test_import_warns_when_a_higher_priority_copy_shadows_it(dirs):
    src = _write_profile(dirs["tmp"] / "demo_copy.json",
                         display_name="Demo (imported)")
    _profile, _target, warnings = prof.import_profile_file(str(src))
    assert any("shadow" in w for w in warnings)
    # The env copy still wins.
    assert prof.load_profiles()["demo"].display_name == "Demo Project"


def test_import_rejects_a_file_that_is_not_a_profile(dirs):
    bad = dirs["tmp"] / "bad.json"
    bad.write_text("{\"display_name\": \"no name\"}", encoding="utf-8")
    with pytest.raises(prof.ProfileError):
        prof.import_profile_file(str(bad))
    assert not (dirs["user"] / "bad.json").exists()
    garbage = dirs["tmp"] / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    with pytest.raises(prof.ProfileError):
        prof.import_profile_file(str(garbage))


def test_the_shipped_profile_is_complete_and_launchable_as_data():
    shipped = REPO / "profiles" / "ttlc.json"
    profile = prof.read_profile_file(str(shipped))
    assert profile.name == "ttlc"
    assert not profile.is_template, "the shipped profile must carry real paths"
    assert profile.psetup.executable and profile.psetup.proj and profile.psetup.cfg
    assert profile.tool.executable.endswith("/bin/tessent")
    assert profile.tool.shell_flag == "-shell"
    assert any("LICENSE" in k.upper() for k in profile.environment)
    assert profile.control.allowed_commands, "the live channel stays configured"


# ---------------------------------------------------------------------------
# launch-configuration files
# ---------------------------------------------------------------------------

def test_config_round_trip(tmp_path):
    form = {"profile": "demo", "proj": "p/R", "cfg": "c.cth",
            "paths": {"icl": "/x/a.icl"}, "mode": "individual"}
    path = tmp_path / "sub" / "cfg.json"
    write_launch_config(str(path), "My setup", form)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == CONFIG_KIND and data["name"] == "My setup"
    name, back = read_launch_config(str(path))
    assert (name, back) == ("My setup", form)


def test_config_name_falls_back_to_the_file_stem(tmp_path):
    path = tmp_path / "fuse_hf.json"
    write_launch_config(str(path), "", {"proj": "x"})
    assert read_launch_config(str(path))[0] == "fuse_hf"


def test_a_foreign_json_is_refused(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(PROFILE), encoding="utf-8")
    with pytest.raises(ConfigFileError) as info:
        read_launch_config(str(path))
    assert CONFIG_KIND in str(info.value)
    with pytest.raises(ConfigFileError):
        read_launch_config(str(tmp_path / "missing.json"))


def test_config_file_stem_is_filesystem_safe():
    assert config_file_stem("ttlcdie/TS2026.6_TTLC.HF") == "ttlcdie_TS2026.6_TTLC.HF"
    assert config_file_stem("  ") == "launch_config"


def test_default_config_dir_honours_the_override(dirs):
    assert default_config_dir() == str(dirs["tmp"] / "configs")


# ---------------------------------------------------------------------------
# panel: profiles
# ---------------------------------------------------------------------------

def test_load_profile_button_exists_next_to_the_picker(panel):
    assert panel.load_profile_btn.text().startswith("Load profile")
    assert panel.load_preset_btn.text().startswith("Load")


def test_import_profile_selects_it_and_persists_across_reload(panel, dirs):
    src = _write_profile(dirs["tmp"] / "x.json", name="other",
                         display_name="Other")
    assert panel.import_profile(str(src))
    assert panel.current_profile_name() == "other"
    assert "copied to" in panel.status_label.text()
    panel.reload_profiles()
    assert panel.profile_combo.findData("other") >= 0


def test_import_profile_reports_a_bad_file(panel, dirs):
    bad = dirs["tmp"] / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert not panel.import_profile(str(bad))
    assert panel.status_label.text().startswith("Cannot load profile:")
    assert panel.current_profile_name() == "demo"


def test_on_load_profile_uses_the_picked_file(panel, dirs, monkeypatch):
    src = _write_profile(dirs["tmp"] / "picked.json", name="picked")
    monkeypatch.setattr(vp_mod.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(src), "")))
    panel.on_load_profile()
    assert panel.current_profile_name() == "picked"


def test_on_load_profile_cancelled_does_nothing(panel, monkeypatch):
    monkeypatch.setattr(vp_mod.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    panel.on_load_profile()
    assert panel.current_profile_name() == "demo"


# ---------------------------------------------------------------------------
# panel: configurations
# ---------------------------------------------------------------------------

def test_save_to_file_writes_and_lists_under_the_stem(panel, dirs):
    panel.proj_edit.setText("proj/A")
    target = dirs["tmp"] / "keep" / "fuse_hf.json"
    assert panel.save_preset_to_file(str(target))
    assert target.is_file()
    assert panel.current_preset_name() == "fuse_hf"
    assert panel.preset_file("fuse_hf") == str(target)
    assert "fuse_hf" in panel.preset_names()


def test_load_from_file_applies_the_form(panel, dirs):
    panel.proj_edit.setText("proj/A")
    panel.cfg_edit.setText("a.cth")
    path = dirs["tmp"] / "a.json"
    panel.save_preset_to_file(str(path), name="setup A")
    panel.proj_edit.setText("proj/B")
    panel.cfg_edit.setText("b.cth")
    assert panel.load_preset_from_file(str(path))
    assert panel.proj_edit.text() == "proj/A"
    assert panel.cfg_edit.text() == "a.cth"
    assert panel.current_preset_name() == "setup A"


def test_load_from_file_refuses_a_profile_file(panel, dirs):
    assert not panel.load_preset_from_file(str(dirs["env"] / "demo.json"))
    assert panel.status_label.text().startswith("Cannot load configuration:")


def test_delete_forgets_the_entry_but_keeps_the_file(panel, dirs):
    path = dirs["tmp"] / "d.json"
    panel.save_preset_to_file(str(path))
    assert panel.delete_preset("d")
    assert path.is_file()
    assert panel.preset_file("d") == ""


def test_file_origins_survive_export_import(panel, dirs):
    path = dirs["tmp"] / "e.json"
    panel.save_preset_to_file(str(path))
    blob = panel.export_settings()
    assert blob["preset_files"] == {"e": str(path)}
    fresh = VisualizerPanel()
    try:
        fresh.reload_profiles()
        fresh.import_settings(blob)
        assert fresh.preset_file("e") == str(path)
    finally:
        fresh.stop_log_tail()


def test_on_save_preset_asks_for_a_location_in_the_config_dir(panel, dirs,
                                                              monkeypatch):
    panel.proj_edit.setText("ttlcdie/TS2026.6_TTLC.HF")
    seen = {}

    def fake_save(parent, title, suggested, filt):
        seen["suggested"] = suggested
        return str(dirs["tmp"] / "chosen"), ""

    monkeypatch.setattr(vp_mod.QFileDialog, "getSaveFileName",
                        staticmethod(fake_save))
    panel.on_save_preset()
    assert seen["suggested"].startswith(str(dirs["tmp"] / "configs"))
    assert seen["suggested"].endswith("ttlcdie_TS2026.6_TTLC.HF.json")
    # A missing extension is added, and the entry is listed by the stem.
    assert (dirs["tmp"] / "chosen.json").is_file()
    assert panel.current_preset_name() == "chosen"


def test_on_save_preset_reuses_the_selected_entry_file(panel, dirs, monkeypatch):
    path = dirs["tmp"] / "again.json"
    panel.save_preset_to_file(str(path))
    seen = {}

    def fake_save(parent, title, suggested, filt):
        seen["s"] = suggested
        return "", ""

    monkeypatch.setattr(vp_mod.QFileDialog, "getSaveFileName",
                        staticmethod(fake_save))
    panel.on_save_preset()
    assert seen["s"] == str(path)


def test_on_load_preset_applies_the_picked_file(panel, dirs, monkeypatch):
    panel.proj_edit.setText("proj/Z")
    path = dirs["tmp"] / "z.json"
    panel.save_preset_to_file(str(path))
    panel.proj_edit.setText("proj/other")
    monkeypatch.setattr(vp_mod.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    panel.on_load_preset()
    assert panel.proj_edit.text() == "proj/Z"


def test_help_documents_load_profile_and_config_files():
    from atpg_coverage_debug_agent.gui.main_window import _HELP_HTML
    assert "Load\n    profile" in _HELP_HTML or "Load profile" in _HELP_HTML
    assert "visualizer_configs" in _HELP_HTML
    assert "you choose the location of" in _HELP_HTML
