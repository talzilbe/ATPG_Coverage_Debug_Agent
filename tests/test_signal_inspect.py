"""Offscreen tests for 'Open signal in Tessent Visualizer'.

The menu has one correctness rule that matters more than the rest: a cluster
prefix is a string the triage computed, not an object in the design, so it must
never be offered to the tool.

The menus are tested through their *model* rather than by showing them.
``QMenu.exec`` is a C++ slot that cannot be monkeypatched, so a test that tries
to drive a real menu simply blocks forever.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.gui.triage_panel import TriagePanel
from atpg_coverage_debug_agent.gui.visualizer_panel import VisualizerPanel
from atpg_coverage_debug_agent.launcher import LiveSessionError

FAULTS = "\n".join(
    [f"AU.TC 1 top/core/fscan/tdr_reg/u{i}/Y" for i in range(40)]
    + [f"UO.AAB 0 top/core/crypto/aes/u{i}/Y" for i in range(20)]
    + [f"DS 0 top/misc/blk/u{i}/Y" for i in range(60)]
)

PROFILE = {
    "name": "demo",
    "display_name": "Demo",
    "psetup": {"executable": "/bin/echo", "proj": "p/1"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "load": [{"key": "faults", "command": "read_faults", "label": "Faults"}],
        "open": "open_visualizer",
        "signal_inspect": [
            {"verb": "add_schematic_objects", "label": "Flat schematic",
             "options": {"-display": "flat_schematic"}},
            {"verb": "add_schematic_objects", "label": "Hierarchical",
             "options": {"-display": "hierarchical_schematic"}},
        ],
    },
    "control": {"enabled": True,
                "allowed_commands": ["add_schematic_objects"],
                "allowed_options": ["-display"]},
}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "demo.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(directory))
    return directory


@pytest.fixture()
def panel(qapp, profile_env):
    widget = VisualizerPanel()
    widget.reload_profiles()
    yield widget
    widget.stop_log_tail()
    widget._session_timer.stop()


@pytest.fixture()
def report(tmp_path, sample_netlist_path):
    faults = tmp_path / "dotted.faults"
    faults.write_text(FAULTS, encoding="utf-8")
    return run_analysis(sample_netlist_path, str(faults), None)


@pytest.fixture()
def triage(qapp, report):
    widget = TriagePanel()
    widget.set_report(report)
    return widget


class _FakeSession:
    """Stands in for a live tool session."""

    def __init__(self, reply="", error=""):
        self.reply = reply
        self.error = error
        self.sent = []
        self.port = 4242

    def send(self, action, obj):
        self.sent.append((action, obj))
        if self.error:
            raise LiveSessionError(self.error)
        return self.reply


def _first_fault_leaf(tree):
    for r in range(tree.topLevelItemCount()):
        root = tree.topLevelItem(r)
        for i in range(root.childCount()):
            node = root.child(i)
            for j in range(node.childCount()):
                leaf = node.child(j)
                if leaf.data(1, Qt.UserRole) == "fault":
                    return leaf
    return None


def _first_prefix_node(tree):
    root = tree.topLevelItem(0)
    return root.child(0) if root is not None and root.childCount() else None


# ---------------------------------------------------------------------------
# The panel's side: sending to the session
# ---------------------------------------------------------------------------
def test_the_actions_come_from_the_profile(panel):
    actions = panel.signal_actions()
    assert [a.label for a in actions] == ["Flat schematic", "Hierarchical"]
    assert actions[0].options == {"-display": "flat_schematic"}


def test_showing_a_signal_sends_it_to_the_session(panel):
    panel._session = _FakeSession(reply="")
    assert panel.show_signal("/top/u_a/Q")
    action, obj = panel._session.sent[0]
    assert obj == "/top/u_a/Q"
    assert action.verb == "add_schematic_objects"


def test_a_chosen_action_is_used_instead_of_the_default(panel):
    panel._session = _FakeSession()
    second = panel.signal_actions()[1]
    panel.show_signal("/top/u_a/Q", second)
    assert panel._session.sent[0][0].options["-display"] == "hierarchical_schematic"


def test_without_a_session_it_refuses_and_shows_the_command(panel):
    assert not panel.show_signal("/top/u_a/Q")
    text = panel.status_label.text()
    assert "No live session" in text
    assert "add_schematic_objects {/top/u_a/Q}" in text


def test_a_tool_error_is_surfaced_verbatim(panel):
    panel._session = _FakeSession(error="may only be used after flattening")
    assert not panel.show_signal("/top/u_a/Q")
    assert "may only be used after flattening" in panel.status_label.text()


def test_a_profile_without_inspect_actions_says_so(panel, profile_env):
    bare = dict(PROFILE, name="bare")
    bare["commands"] = dict(PROFILE["commands"])
    bare["commands"]["signal_inspect"] = []
    (profile_env / "bare.json").write_text(json.dumps(bare), encoding="utf-8")
    panel.reload_profiles()
    panel.select_profile("bare")
    assert not panel.show_signal("/top/u_a/Q")
    assert "no way to show an object" in panel.status_label.text()


def test_the_session_indicator_starts_empty(panel):
    assert "No live session" in panel.session_label.text()
    assert not panel.has_live_session()


def test_a_profile_without_a_channel_is_stated(panel):
    panel._begin_session(SimpleNamespace(token="", port_file=""))
    assert "disables it" in panel.session_label.text()


def test_the_indicator_reports_the_port_once_it_appears(panel, tmp_path):
    port_file = tmp_path / "control.port"
    panel._begin_session(SimpleNamespace(token="tok", port_file=str(port_file)))
    panel._poll_session()
    assert "starting" in panel.session_label.text()

    port_file.write_text("5555", encoding="utf-8")
    panel._poll_session()
    assert "5555" in panel.session_label.text()
    assert panel.has_live_session()


# ---------------------------------------------------------------------------
# The triage panel's side: which rows offer the action
# ---------------------------------------------------------------------------
def test_a_fault_sample_offers_the_action(triage):
    leaf = _first_fault_leaf(triage.cluster_tree)
    assert leaf is not None, "no fault sample in the tree"
    path, rows = triage._cluster_menu_model(leaf)
    entries = {label: enabled for label, enabled, _id in rows}
    assert entries["Open signal in Tessent Visualizer"] is True
    assert path == leaf.data(0, Qt.UserRole)


def test_choosing_it_emits_the_object(triage):
    leaf = _first_fault_leaf(triage.cluster_tree)
    seen = []
    triage.signal_inspect_requested.connect(seen.append)
    path, _rows = triage._cluster_menu_model(leaf)
    triage._run_menu_action("show", path)
    assert seen == [path]


def test_a_cluster_prefix_cannot_be_sent_to_the_tool(triage):
    """A prefix is derived, not a design object; it would not resolve."""
    node = _first_prefix_node(triage.cluster_tree)
    assert node is not None and node.data(1, Qt.UserRole) != "fault"
    _path, rows = triage._cluster_menu_model(node)
    entries = {label: enabled for label, enabled, _id in rows}
    assert entries["Open signal in Tessent Visualizer"] is False


def test_a_prefix_row_still_offers_copy(triage):
    node = _first_prefix_node(triage.cluster_tree)
    _path, rows = triage._cluster_menu_model(node)
    assert ("Copy path", True, "copy") in rows


def test_focus_is_only_offered_on_a_real_fault(triage):
    leaf = _first_fault_leaf(triage.cluster_tree)
    node = _first_prefix_node(triage.cluster_tree)
    assert any(i == "focus" for _l, _e, i in triage._cluster_menu_model(leaf)[1])
    assert not any(i == "focus" for _l, _e, i
                   in triage._cluster_menu_model(node)[1])


def test_blocking_signals_are_collected_and_deduplicated(triage):
    category = SimpleNamespace(attribution=SimpleNamespace(
        tie_sources=[SimpleNamespace(driver="/top/u_tie/Y"),
                     SimpleNamespace(driver="/top/u_tie/Y")],
        constraint_sources=[SimpleNamespace(signal="/top/pi_hold")]))
    assert triage._blocking_signals(category) == ["/top/u_tie/Y", "/top/pi_hold"]


def test_a_category_without_attribution_yields_nothing(triage):
    assert triage._blocking_signals(SimpleNamespace(attribution=None)) == []


def test_each_blocking_signal_becomes_its_own_entry(triage):
    category = SimpleNamespace(attribution=SimpleNamespace(
        tie_sources=[SimpleNamespace(driver="/top/u_tie/Y")],
        constraint_sources=[SimpleNamespace(signal="/top/pi_hold")]))
    _path, rows = triage._category_menu_model(category)
    ids = [action_id for _l, _e, action_id in rows]
    assert ids == ["show:/top/u_tie/Y", "show:/top/pi_hold"]

    seen = []
    triage.signal_inspect_requested.connect(seen.append)
    triage._run_menu_action(ids[1], "")
    assert seen == ["/top/pi_hold"]


def test_a_category_with_no_blocking_signal_says_so(triage):
    _path, rows = triage._category_menu_model(SimpleNamespace(attribution=None))
    assert rows == [("No blocking signal was identified", False, "none")]


def test_a_fix_proposal_offers_its_hotspot(triage):
    if not triage._recommendations:
        pytest.skip("no recommendations in this report")
    triage._recommendations[0].hotspot = "/top/core/fscan"
    hotspot, rows = triage._fix_menu_model(0)
    assert hotspot == "/top/core/fscan"
    assert rows[0][1] is True and rows[0][2] == "show"

    seen = []
    triage.signal_inspect_requested.connect(seen.append)
    triage._run_menu_action("show", hotspot)
    assert seen == ["/top/core/fscan"]


def test_a_fix_proposal_without_a_hotspot_says_so(triage):
    if not triage._recommendations:
        pytest.skip("no recommendations in this report")
    triage._recommendations[0].hotspot = ""
    _hotspot, rows = triage._fix_menu_model(0)
    assert rows == [("This proposal names no hotspot path", False, "none")]


def test_a_row_that_does_not_exist_yields_no_menu(triage):
    assert triage._fix_menu_model(999) == ("", [])
