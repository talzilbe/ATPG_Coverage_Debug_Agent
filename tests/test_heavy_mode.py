"""Heavy mode: the Tessent session runs on a Netbatch machine.

The only intended difference from a normal launch is how the tool is started,
so most of these tests pin that the normal chain is untouched, and that the
remote control channel stays as closed as the network allows.
"""

from __future__ import annotations

import os
import socket

import pytest

from atpg_coverage_debug_agent.launcher import live_session as ls
from atpg_coverage_debug_agent.launcher import visualizer as vis
from atpg_coverage_debug_agent.launcher.live_session import (
    LiveSession, LiveSessionError, build_listener_tcl, new_token,
    read_endpoint, read_port,
)
from atpg_coverage_debug_agent.launcher.profiles import ToolProfile
from atpg_coverage_debug_agent.launcher.terminals import TerminalSpec
from atpg_coverage_debug_agent.launcher.visualizer import (
    HEAVY_NBJOB_ARGV, LaunchInputError, VisualizerInputs, describe_chain,
    heavy_command, heavy_display, launch_health, write_launch_bundle,
)

EXPECTED_NBJOB = ("nbjob run --target zsc28_express --qslot "
                  "/cdg/be/ttl/compute/dfx --class 'SLES15SP4&&1C&&2047G' "
                  "/bin/csh")

PROFILE = {
    "name": "heavy_demo",
    "display_name": "Heavy Demo",
    "shell": "/bin/tcsh",
    "psetup": {"executable": "/bin/echo", "proj": "p/1", "command_flag": "-x"},
    "environment": {"DEMO_LICENSE_SERVER": "1717@licsrv.example.com"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "load": [{"key": "faults", "command": "read_faults", "label": "Faults"}],
        "open": "open_visualizer",
    },
    "control": {"enabled": True, "allow_agent_eval": True,
                "allowed_commands": ["add_schematic_objects"],
                "allowed_options": ["-display"]},
}

PEER = "10.1.2.3"


@pytest.fixture()
def parts(tmp_path, monkeypatch):
    faults = tmp_path / "f.gz"
    faults.write_text("x", encoding="utf-8")
    monkeypatch.setenv("DISPLAY", "myhost.example.com:5")
    monkeypatch.setattr(vis, "local_addresses", lambda: [PEER])
    profile = ToolProfile.from_dict(PROFILE)
    inputs = VisualizerInputs(proj="p/1", paths={"faults": str(faults)})
    term = TerminalSpec("xterm", "-e", "-title", executable="/usr/bin/xterm")
    return profile, inputs, term


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# The submission line
# ---------------------------------------------------------------------------
def test_the_submission_line_is_the_requested_nbjob_command():
    assert heavy_command("x.csh") == EXPECTED_NBJOB + " x.csh"
    assert HEAVY_NBJOB_ARGV[:2] == ("nbjob", "run")


def test_a_local_display_is_qualified_with_this_host():
    assert heavy_display(":3") == socket.getfqdn() + ":3"
    assert heavy_display("box.example.com:7.0") == "box.example.com:7.0"


@pytest.mark.parametrize("value", ["", "localhost:10.0", "127.0.0.1:2",
                                   "box;rm:1"])
def test_an_unreachable_display_is_refused(value):
    assert heavy_display(value) == ""


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------
def test_normal_mode_is_unchanged(parts, tmp_path):
    profile, inputs, term = parts
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    psetup = _read(bundle.psetup_script_path)
    assert "nbjob" not in psetup
    assert vis.TESSENT_SCRIPT_NAME in psetup
    assert not bundle.heavy and not bundle.submit_script_path
    assert not os.path.exists(os.path.join(bundle.directory,
                                           vis.HEAVY_SUBMIT_SCRIPT_NAME))
    assert "-myaddr 127.0.0.1" in _read(bundle.dofile_path)
    assert bundle.agent_eval


def test_heavy_mode_submits_the_same_inner_script_through_netbatch(parts,
                                                                   tmp_path):
    profile, inputs, term = parts
    inputs.heavy = True
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    assert bundle.heavy

    psetup = _read(bundle.psetup_script_path)
    assert vis.HEAVY_SUBMIT_SCRIPT_NAME in psetup
    assert vis.TESSENT_SCRIPT_NAME not in psetup

    submit = _read(bundle.submit_script_path)
    assert (EXPECTED_NBJOB + f" '{bundle.remote_script_path}'") in submit

    remote = _read(bundle.remote_script_path)
    assert "setenv DISPLAY 'myhost.example.com:5'" in remote
    assert f"-e '{bundle.tessent_script_path}'" in remote
    assert remote.splitlines()[-1].startswith("exec /usr/bin/xterm")

    # The tool itself is started exactly as in normal mode.
    normal = write_launch_bundle(profile, VisualizerInputs(
        proj=inputs.proj, paths=inputs.paths), str(tmp_path / "n"), term)
    assert (_read(bundle.tessent_script_path).replace(bundle.directory, "D")
            == _read(normal.tessent_script_path).replace(normal.directory, "D"))


def test_heavy_mode_listener_accepts_only_this_machine_and_keeps_agent_eval(
        parts, tmp_path):
    profile, inputs, term = parts
    inputs.heavy = True
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    dofile = _read(bundle.dofile_path)
    assert "-myaddr" not in dofile
    assert PEER in dofile
    assert "lsearch -exact $::atpg_peers $addr" in dofile
    assert "set ::atpg_allow_eval 1" in dofile
    assert bundle.agent_eval
    assert bundle.token and bundle.port_file


def test_heavy_mode_writes_to_a_shared_directory_not_tmp(parts, tmp_path):
    profile, inputs, _term = parts
    ward = tmp_path / "ward"
    ward.mkdir()
    inputs.ward = str(ward)
    inputs.heavy = True
    assert vis.heavy_bundle_dir(inputs).startswith(
        str(ward / ".atpg_visualizer"))
    inputs.ward = ""
    assert vis.heavy_bundle_dir(inputs).startswith(
        os.path.join(os.path.expanduser("~"), ".atpg_debug_agent"))


def test_heavy_mode_refuses_a_forwarded_display(parts, tmp_path, monkeypatch):
    profile, inputs, term = parts
    inputs.heavy = True
    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    with pytest.raises(LaunchInputError):
        write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)


def test_heavy_mode_refuses_when_this_machine_has_no_address(parts, tmp_path,
                                                             monkeypatch):
    profile, inputs, term = parts
    inputs.heavy = True
    monkeypatch.setattr(vis, "local_addresses", lambda: [])
    with pytest.raises(LaunchInputError):
        write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)


def test_the_chain_and_health_mention_netbatch_only_in_heavy_mode(parts):
    profile, inputs, _term = parts
    assert not any("nbjob" in s for s in describe_chain(profile, inputs))
    labels = {c.label for c in launch_health(profile, inputs)}
    assert "Netbatch (nbjob)" not in labels
    inputs.heavy = True
    assert any(s.startswith("nbjob run") for s in describe_chain(profile, inputs))
    labels = {c.label for c in launch_health(profile, inputs)}
    assert {"Netbatch (nbjob)", "Display for the heavy machine"} <= labels


def test_heavy_flag_round_trips_through_inputs():
    data = VisualizerInputs(heavy=True).as_dict()
    assert VisualizerInputs.from_dict(data).heavy
    assert not VisualizerInputs.from_dict({}).heavy


# ---------------------------------------------------------------------------
# The control channel
# ---------------------------------------------------------------------------
def test_the_default_listener_is_still_loopback_only():
    tcl = build_listener_tcl("/tmp/p", new_token(), ["add_schematic_objects"],
                             [])
    assert "-myaddr 127.0.0.1" in tcl
    assert 'string equal $addr "127.0.0.1"' in tcl


def test_the_remote_listener_records_its_host_and_rejects_bad_peers():
    tcl = build_listener_tcl("/tmp/p", new_token(), ["add_schematic_objects"],
                             [], allowed_peers=[PEER])
    assert "[info hostname] $::atpg_port" in tcl
    assert "::ffff:" + PEER in tcl
    with pytest.raises(LiveSessionError):
        build_listener_tcl("/tmp/p", new_token(), [], [],
                           allowed_peers=["10.0.0.1 ; exec rm"])


def test_the_port_file_may_name_a_host(tmp_path):
    path = tmp_path / "control.port"
    path.write_text("4242\n", encoding="utf-8")
    assert read_endpoint(str(path)) == ("127.0.0.1", 4242)
    path.write_text("heavyhost.example.com 4242\n", encoding="utf-8")
    assert read_endpoint(str(path)) == ("heavyhost.example.com", 4242)
    assert read_port(str(path)) == 4242
    session = LiveSession(str(path), "tok")
    assert session.host == "heavyhost.example.com"
    path.write_text("bad;host 4242\n", encoding="utf-8")
    assert read_endpoint(str(path)) is None


def test_local_addresses_never_include_loopback():
    assert all(not a.startswith("127.") and a != "::1"
               for a in ls.local_addresses())


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------
def test_the_panel_toggle_reaches_inputs_and_is_saved(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    import json
    from PySide6.QtWidgets import QApplication
    from atpg_coverage_debug_agent.gui.visualizer_panel import VisualizerPanel

    QApplication.instance() or QApplication([])
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "heavy_demo.json").write_text(json.dumps(PROFILE),
                                               encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(directory))
    panel = VisualizerPanel()
    panel.reload_profiles()

    assert not panel.heavy_check.isChecked()
    assert not panel.heavy_note.isVisibleTo(panel)
    assert not panel.current_inputs().heavy

    panel.heavy_check.setChecked(True)
    assert panel.heavy_note.isVisibleTo(panel)
    assert "nbjob run" in panel.heavy_note.text()
    assert panel.current_inputs().heavy
    saved = panel.export_settings()
    assert saved["heavy_mode"] is True

    panel.heavy_check.setChecked(False)
    panel.import_settings(saved)
    assert panel.heavy_check.isChecked()
    panel.import_settings({"profile": "heavy_demo"})
    assert not panel.heavy_check.isChecked()
