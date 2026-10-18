"""Filling the fault list and constraints in from the netlist's run folder."""

from __future__ import annotations

import os
import time

import pytest

from atpg_coverage_debug_agent.analysis.input_discovery import (
    design_stem, discover_inputs, find_constraints, find_fault_list,
)

FAULT_FILES = [
    "blk.edt.sa.a0.atpg.faults.fd.gz",
    "blk.edt.sa.a0.atpg.faults.fd.del.gz",
    "blk.edt.sa.a0.atpg.ph1.faults.gz",
    "blk.edt.sa.a0.atpg.ph6.faults.gz",
    "blk.faults.gz",
    "blk.detected.faults.gz",
    "blk.basic_disp_faults.gz",
    "disp_faults_orig.gz",
    "faults.dt.gz",
    ".blk.au.disposition.faults.gz",
]


def _touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return str(path)


def _sage_tree(root, files=FAULT_FILES, recipe="stuckat_edt_min"):
    """WORK/<recipe>/{netlist,faultlist} + WORK/proj_common/atpg_files."""
    work = os.path.join(str(root), "sage_atpg", "sage", "WORK")
    for name in files:
        _touch(os.path.join(work, recipe, "faultlist", name))
    for name in ("blk.do", "user_atpg.do", "global_init.do", "procs.tcl"):
        _touch(os.path.join(work, "proj_common", "atpg_files", name))
    return work


@pytest.mark.parametrize("name,stem", [
    ("blk.v.gz", "blk"), ("blk.v", "blk"), ("top.vg", "top"),
    ("x.y.v.bz2", "x.y"), ("plain", "plain"),
])
def test_the_design_name_comes_from_the_netlist_file(name, stem):
    assert design_stem(f"/a/{name}") == stem


def test_the_final_list_is_chosen_from_a_recipe_netlist(tmp_path):
    work = _sage_tree(tmp_path)
    netlist = _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v.gz"))
    choice = find_fault_list(netlist)
    assert os.path.basename(choice.path) == "blk.edt.sa.a0.atpg.faults.fd.gz"
    assert "final fault list" in choice.reason and ".fd" in choice.reason
    names = {os.path.basename(p) for p in choice.alternatives}
    assert names == {"blk.edt.sa.a0.atpg.ph1.faults.gz",
                     "blk.edt.sa.a0.atpg.ph6.faults.gz", "blk.faults.gz"}
    assert "3 other fault list(s)" in choice.note
    assert choice.directory in choice.note


def test_a_netlist_above_the_work_area_is_found_too(tmp_path):
    _sage_tree(tmp_path)
    netlist = _touch(os.path.join(str(tmp_path), "sage_atpg", "blk.v"))
    choice = find_fault_list(netlist)
    assert choice.path.endswith("blk.edt.sa.a0.atpg.faults.fd.gz")


def test_partial_lists_are_never_chosen(tmp_path):
    work = _sage_tree(tmp_path, files=["blk.edt.sa.a0.atpg.faults.fd.del.gz",
                                       "blk.detected.faults.gz",
                                       "blk.basic_disp_faults.gz"])
    netlist = _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v.gz"))
    assert find_fault_list(netlist) is None


def test_without_a_final_list_the_latest_phase_wins(tmp_path):
    work = _sage_tree(tmp_path, files=["blk.edt.sa.a0.atpg.ph1.faults.gz",
                                       "blk.edt.sa.a0.atpg.ph6.faults.gz",
                                       "blk.faults.gz"])
    netlist = _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v.gz"))
    choice = find_fault_list(netlist)
    assert choice.path.endswith("ph6.faults.gz")
    assert "phase 6" in choice.reason


def test_another_designs_lists_are_not_taken_from_above_the_netlist(tmp_path):
    work = _sage_tree(tmp_path, files=["other.faults.fd.gz"])
    netlist = _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v.gz"))
    assert find_fault_list(netlist) is None


def test_the_recipe_with_the_best_list_wins_and_the_others_are_named(tmp_path):
    work = _sage_tree(tmp_path, files=["blk.edt.sa.a0.atpg.ph2.faults.gz"],
                      recipe="a_recipe")
    _sage_tree(tmp_path, files=["blk.edt.sa.a0.atpg.faults.fd.gz"],
               recipe="b_recipe")
    netlist = _touch(os.path.join(str(tmp_path), "sage_atpg", "blk.v"))
    choice = find_fault_list(netlist)
    assert "b_recipe" in choice.path
    assert any("a_recipe" in d for d in choice.other_dirs)
    assert "1 other fault-list folder" in choice.note


def test_the_constraints_are_the_dofile_named_after_the_design(tmp_path):
    work = _sage_tree(tmp_path)
    netlist = _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v.gz"))
    choice = find_constraints(netlist)
    assert choice.path.endswith(os.path.join("proj_common", "atpg_files", "blk.do"))
    names = {os.path.basename(p) for p in choice.alternatives}
    assert names == {"user_atpg.do", "global_init.do", "procs.tcl"}
    assert "named after the design 'blk'" in choice.note


def test_nothing_nearby_gives_nothing(tmp_path):
    netlist = _touch(os.path.join(str(tmp_path), "lonely", "blk.v"))
    found = discover_inputs(netlist)
    assert found.faults is None and found.constraints is None
    assert found.stem == "blk"


def test_the_climb_is_bounded(tmp_path):
    work = _sage_tree(tmp_path)
    deep = os.path.join(work, "stuckat_edt_min", "a", "b", "c", "d", "blk.v")
    assert find_fault_list(_touch(deep)) is None


# ---------------------------------------------------------------------------
# The input boxes
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from atpg_coverage_debug_agent.config import settings as settings_mod
    from atpg_coverage_debug_agent.gui import main_window as mw
    from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(settings_mod, "_DEFAULT_CONFIG_FILE",
                        tmp_path / "settings.json")
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    win = mw.MainWindow()
    win._discovery_sync = True
    monkeypatch.setenv("ATPG_INPUT_DISCOVERY", "1")
    yield win
    win.agent_panel.shutdown()
    win.close()


@pytest.fixture()
def netlist(tmp_path):
    work = _sage_tree(tmp_path)
    return _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v"),
                  "module blk(a); input a; endmodule\n")


def test_a_netlist_fills_both_boxes_with_a_note(window, netlist):
    window.netlist_picker.set_path(netlist)
    faults = window.faults_picker
    assert faults.path().endswith("blk.edt.sa.a0.atpg.faults.fd.gz")
    assert faults.is_auto
    assert not faults.note.isHidden() and "final fault list" in faults.note.text()
    assert not faults.alt_btn.isHidden()
    constraints = window.constraints_picker
    assert constraints.path().endswith("blk.do")
    assert not constraints.note.isHidden()
    assert constraints.alt_btn.isHidden() and constraints.alt_menu_model() == []
    assert "Pick another" not in constraints.note.text()


def test_a_new_netlist_replaces_the_previous_designs_paths(window, netlist, tmp_path):
    old = _touch(os.path.join(str(tmp_path), "old.faults"))
    window.faults_picker.set_path(old)
    window.netlist_picker.set_path(netlist)
    assert window.faults_picker.path().endswith("blk.edt.sa.a0.atpg.faults.fd.gz")
    assert not window.faults_picker.note.isHidden()


def test_with_the_option_off_a_path_the_user_set_is_kept(window, netlist, tmp_path):
    window.autofill_check.setChecked(False)
    mine = _touch(os.path.join(str(tmp_path), "mine.faults"))
    window.faults_picker.set_path(mine)
    window.netlist_picker.set_path(netlist)
    assert window.faults_picker.path() == mine
    assert window.faults_picker.note.isHidden()
    assert window.constraints_picker.path().endswith("blk.do")


def test_the_same_netlist_again_keeps_the_users_choice(window, netlist, tmp_path):
    window.netlist_picker.set_path(netlist)
    mine = _touch(os.path.join(str(tmp_path), "mine.faults"))
    window.faults_picker.set_path(mine)
    window._discovered_for = ""
    window.netlist_picker.set_path(netlist)
    assert window.faults_picker.path() == mine


def test_files_dropped_with_the_netlist_are_kept(window, netlist, tmp_path):
    mine = _touch(os.path.join(str(tmp_path), "mine.faults"))
    window.netlist_picker.set_path(netlist)
    window.faults_picker.set_path(mine)
    window._inputs_netlist = netlist
    found = discover_inputs(netlist)
    window._apply_discovery(netlist, found)
    assert window.faults_picker.path() == mine


def test_the_option_is_saved(window):
    window.autofill_check.setChecked(False)
    assert window._settings.auto_fill_inputs is False


def test_ticking_the_option_fills_from_the_current_netlist(window, netlist, tmp_path):
    window.netlist_picker.set_path(netlist)
    mine = _touch(os.path.join(str(tmp_path), "mine.faults"))
    window.faults_picker.set_path(mine)
    window.autofill_check.setChecked(False)
    window.autofill_check.setChecked(True)
    assert window.faults_picker.path().endswith("blk.edt.sa.a0.atpg.faults.fd.gz")
    assert window.constraints_picker.path().endswith("blk.do")


def test_editing_the_filled_path_hides_the_reason(window, netlist):
    window.netlist_picker.set_path(netlist)
    faults = window.faults_picker
    auto = faults.path()
    other = os.path.join(os.path.dirname(auto), "blk.faults.gz")
    faults.set_path(other)
    assert faults.note.isHidden() and not faults.is_auto
    assert not faults.alt_btn.isHidden()
    labels = [label for label, _ in faults.alt_menu_model()]
    assert any("(auto choice)" in label for label in labels)
    faults.set_path("/elsewhere/x.faults")
    assert faults.alt_btn.isHidden()


def test_pick_another_lists_the_other_lists(window, netlist):
    window.netlist_picker.set_path(netlist)
    model = window.faults_picker.alt_menu_model()
    names = {os.path.basename(path) for _, path in model}
    assert names == {"blk.edt.sa.a0.atpg.ph1.faults.gz",
                     "blk.edt.sa.a0.atpg.ph6.faults.gz", "blk.faults.gz"}


def test_a_new_netlist_refills_only_auto_filled_boxes(window, netlist, tmp_path):
    window.netlist_picker.set_path(netlist)
    other_root = tmp_path / "second"
    work = _sage_tree(other_root)
    second = _touch(os.path.join(work, "stuckat_edt_min", "netlist", "blk.v"),
                    "module blk(a); input a; endmodule\n")
    window.netlist_picker.set_path(second)
    assert window.faults_picker.path().startswith(str(other_root))


def test_a_stale_search_result_is_ignored(window, netlist):
    found = discover_inputs(netlist)
    window.netlist_picker.edit.setText("/some/other.v")
    assert window._apply_discovery(netlist, found) == []
    assert window.faults_picker.path() == ""


def test_the_search_can_be_switched_off(window, netlist, monkeypatch):
    monkeypatch.setenv("ATPG_INPUT_DISCOVERY", "0")
    window.netlist_picker.set_path(netlist)
    assert window.faults_picker.path() == ""
