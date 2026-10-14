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


# ---------------------------------------------------------------------------
# The Netbatch job: id, state, cancel
# ---------------------------------------------------------------------------
SUBMIT_OUTPUT = ("Your job has been queued (JobID 17785443, Class "
                 "SLES15SP4&&1C&&2047G, Queue zsc28_express, Slot "
                 "/cdg/be/ttl/compute/dfx)\n")

STATUS_WAIT = """\tReportingService on sc2_Production
[
{
\t"Jobid" : "17785443",
\t"Status" : "Wait",
\t"WaitReason" : "not candidate yet"
}
]
"""


def test_the_submit_script_keeps_nbjob_output_for_the_job_id(parts, tmp_path):
    profile, inputs, term = parts
    inputs.heavy = True
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    submit = _read(bundle.submit_script_path)
    assert bundle.job_file == os.path.join(bundle.directory, vis.HEAVY_JOB_FILE)
    assert f"|& tee '{bundle.job_file}'" in submit
    assert (EXPECTED_NBJOB + f" '{bundle.remote_script_path}'") in submit
    psetup = _read(bundle.psetup_script_path)
    assert "Session ended" not in psetup
    assert "may wait in the queue" in psetup


def test_a_relaunch_drops_the_previous_job_file(parts, tmp_path):
    profile, inputs, term = parts
    inputs.heavy = True
    dest = tmp_path / "b"
    dest.mkdir()
    (dest / vis.HEAVY_JOB_FILE).write_text(SUBMIT_OUTPUT, encoding="utf-8")
    bundle = write_launch_bundle(profile, inputs, str(dest), term)
    assert vis.read_job_id(bundle.job_file) == ""


def test_job_id_and_status_parsing():
    assert vis.parse_job_id(SUBMIT_OUTPUT) == "17785443"
    assert vis.parse_job_id("nbjob: error") == ""
    assert vis.parse_job_status(STATUS_WAIT, "17785443") == (
        "Wait", "not candidate yet")
    assert vis.parse_job_status("header\n[\n]\n", "17785443") is None
    assert vis.parse_job_status("not json", "17785443") is None
    assert vis.parse_job_status(STATUS_WAIT, "1") is None


def test_netbatch_commands_use_the_submission_target_and_reject_bad_ids():
    assert vis.heavy_target() == "zsc28_express"
    assert vis.job_status_argv("42")[:5] == [
        "nbstatus", "jobs", "--target", "zsc28_express", "jobid==42"]
    assert "json" in vis.job_status_argv("42")
    assert vis.job_remove_argv("42") == [
        "nbjob", "remove", "--target", "zsc28_express", "42"]
    for bad in ("", "42; rm -rf /", "abc"):
        with pytest.raises(LaunchInputError):
            vis.job_remove_argv(bad)
        with pytest.raises(LaunchInputError):
            vis.job_status_argv(bad)


@pytest.fixture()
def heavy_panel(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    import json
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from atpg_coverage_debug_agent.gui import visualizer_panel as vp

    QApplication.instance() or QApplication([])
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "heavy_demo.json").write_text(json.dumps(PROFILE),
                                               encoding="utf-8")
    monkeypatch.setenv("ATPG_TOOL_PROFILES", str(directory))
    panel = vp.VisualizerPanel()
    panel.reload_profiles()
    calls = []
    replies = {}

    def fake_nb(argv, on_done):
        calls.append(list(argv))
        on_done(*replies.get(argv[0], (0, "")))
        return True

    monkeypatch.setattr(panel, "_start_nb", fake_nb)
    job_file = tmp_path / vis.HEAVY_JOB_FILE
    bundle = SimpleNamespace(heavy=True, job_file=str(job_file), token="t",
                             port_file=str(tmp_path / "control.port"),
                             agent_eval=False, log_path="")
    panel._begin_session(bundle)
    return panel, job_file, calls, replies, bundle


def test_the_panel_shows_the_job_and_its_queue_state(heavy_panel):
    panel, job_file, calls, replies, _bundle = heavy_panel
    panel._poll_job()
    assert panel.job_id() == "" and not calls
    assert not panel.cancel_job_btn.isVisibleTo(panel)

    job_file.write_text(SUBMIT_OUTPUT, encoding="utf-8")
    replies["nbstatus"] = (0, "header\n[\n]\n")
    panel._poll_job()
    assert panel.job_id() == "17785443"
    assert panel.has_pending_job()
    assert panel.cancel_job_btn.isVisibleTo(panel)
    assert calls[-1][0] == "nbstatus"
    # Not reported yet right after submission: still pending.
    assert "17785443" in panel.session_label.text()
    assert panel.has_pending_job()

    panel._poll_job()
    assert len(calls) == 1, "status is throttled"

    panel._on_job_status(STATUS_WAIT)
    text = panel.session_label.text()
    assert "Wait" in text and "not candidate yet" in text

    panel._on_job_status(STATUS_WAIT.replace('"Wait"', '"Run"'))
    assert "running" in panel.session_label.text()

    panel._on_job_status("header\n[\n]\n")
    assert "no longer queued" in panel.session_label.text()
    assert not panel.has_pending_job()
    assert not panel.cancel_job_btn.isVisibleTo(panel)


def test_cancelling_removes_the_job(heavy_panel):
    panel, job_file, calls, replies, _bundle = heavy_panel
    job_file.write_text(SUBMIT_OUTPUT, encoding="utf-8")
    replies["nbstatus"] = (0, STATUS_WAIT)
    panel._poll_job()
    replies["nbjob"] = (0, "  +zsc28_tiers2_vp.17785443 (removed successfully)")
    assert panel.cancel_job()
    assert calls[-1] == ["nbjob", "remove", "--target", "zsc28_express",
                         "17785443"]
    assert "cancelled" in panel.session_label.text()
    assert not panel.has_pending_job()
    assert panel._session is None


def test_a_failed_cancel_keeps_the_job(heavy_panel):
    panel, job_file, calls, replies, _bundle = heavy_panel
    job_file.write_text(SUBMIT_OUTPUT, encoding="utf-8")
    panel._poll_job()
    replies["nbjob"] = (1, "permission denied")
    panel.cancel_job()
    assert panel.has_pending_job()
    assert panel.cancel_job_btn.isEnabled()
    assert "permission denied" in panel.status_label.text()


def test_a_live_session_ends_job_tracking(heavy_panel, monkeypatch):
    panel, job_file, calls, replies, _bundle = heavy_panel
    job_file.write_text(SUBMIT_OUTPUT, encoding="utf-8")
    panel._poll_job()
    monkeypatch.setattr(type(panel._session), "endpoint",
                        property(lambda self: ("heavyhost", 4242)))
    panel._poll_session()
    assert not panel.has_pending_job()
    assert not panel.cancel_job_btn.isVisibleTo(panel)
    assert "heavyhost" in panel.session_label.text()
    panel._alive_timer.stop()


@pytest.mark.parametrize("choice, launched", [("abort", False),
                                              ("cancel", True),
                                              ("keep", True)])
def test_launching_with_a_pending_job_asks_first(heavy_panel, monkeypatch,
                                                 tmp_path, choice, launched):
    panel, job_file, calls, replies, _bundle = heavy_panel
    from atpg_coverage_debug_agent.gui import visualizer_panel as vp
    job_file.write_text(SUBMIT_OUTPUT, encoding="utf-8")
    panel._poll_job()
    calls.clear()
    asked = []
    monkeypatch.setattr(panel, "_ask_pending_job",
                        lambda: asked.append(1) or choice)
    monkeypatch.setattr(vp, "missing_inputs", lambda *_a: [])
    monkeypatch.setattr(vp, "find_terminal", lambda *_a: object())
    monkeypatch.setattr(panel, "refresh_health", lambda: [])
    written = []

    def fake_write(*_a, **_k):
        written.append(1)
        raise LaunchInputError("stop here")

    monkeypatch.setattr(vp, "write_launch_bundle", fake_write)
    panel.on_launch()
    assert asked
    assert bool(written) is launched
    assert any(c[:2] == ["nbjob", "remove"] for c in calls) is (choice == "cancel")
