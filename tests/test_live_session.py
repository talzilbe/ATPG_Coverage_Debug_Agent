"""Tests for the live control channel into the running tool session.

This channel makes a design tool execute things, so most of what matters here
is what it *refuses*.  The Tcl half is generated, so the generated text is
asserted too -- a listener that silently lost its token check would otherwise
look exactly like one that kept it.
"""

import json
import socket
import threading

import pytest

from atpg_coverage_debug_agent.launcher import live_session as ls
from atpg_coverage_debug_agent.launcher.live_session import (
    InspectAction, LiveSession, LiveSessionError, build_listener_tcl,
    new_token, read_port, validate_object, validate_option,
)
from atpg_coverage_debug_agent.launcher.profiles import ToolProfile
from atpg_coverage_debug_agent.launcher.visualizer import (
    VisualizerInputs, signal_inspect_actions, write_launch_bundle,
)
from atpg_coverage_debug_agent.launcher.terminals import TerminalSpec

ALLOWED = ["add_schematic_objects", "add_schematic_path"]
OPTIONS = ["-display", "-highlight"]


# ---------------------------------------------------------------------------
# A stand-in for the tool: speaks the same protocol, records what it received.
# ---------------------------------------------------------------------------
class FakeTool:
    """Minimal server implementing the listener's contract."""

    def __init__(self, token, allowed=ALLOWED, options=OPTIONS, reply="OK "):
        self.token = token
        self.allowed = allowed
        self.options = options
        self.reply = reply
        self.received = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(8192).decode("utf-8", "replace").strip()
                self.received.append(data)
                parts = data.split("\t")
                if len(parts) < 3:
                    conn.sendall(b"ERR malformed request\n")
                    continue
                if parts[0] != self.token:
                    conn.sendall(b"ERR bad token\n")
                    continue
                if parts[1] not in self.allowed:
                    conn.sendall(
                        f"ERR command not permitted: {parts[1]}\n".encode())
                    continue
                for pair in parts[3:]:
                    name = pair.split("=", 1)[0]
                    if name not in self.options:
                        conn.sendall(
                            f"ERR option not permitted: {name}\n".encode())
                        break
                else:
                    conn.sendall((self.reply + "\n").encode())

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def token():
    return new_token()


@pytest.fixture()
def tool(token):
    server = FakeTool(token)
    yield server
    server.close()


@pytest.fixture()
def session(tmp_path, tool, token):
    port_file = tmp_path / "control.port"
    port_file.write_text(str(tool.port), encoding="utf-8")
    return LiveSession(str(port_file), token, timeout=5.0)


@pytest.fixture()
def action():
    return InspectAction("add_schematic_objects",
                         {"-display": "flat_schematic", "-highlight": "red"},
                         "Show it")


# ---------------------------------------------------------------------------
# The generated Tcl
# ---------------------------------------------------------------------------
def test_the_listener_binds_loopback_only(token):
    tcl = build_listener_tcl("/tmp/p", token, ALLOWED, OPTIONS)
    assert "-myaddr 127.0.0.1" in tcl
    assert 'string equal $addr "127.0.0.1"' in tcl


def test_the_listener_checks_the_token(token):
    tcl = build_listener_tcl("/tmp/p", token, ALLOWED, OPTIONS)
    assert token in tcl
    assert "bad token" in tcl


def test_the_listener_checks_the_command_against_the_allow_list(token):
    tcl = build_listener_tcl("/tmp/p", token, ALLOWED, OPTIONS)
    assert "lsearch -exact $::atpg_allowed_cmds $verb" in tcl
    assert "command not permitted" in tcl


def test_the_listener_checks_option_names(token):
    tcl = build_listener_tcl("/tmp/p", token, ALLOWED, OPTIONS)
    assert "lsearch -exact $::atpg_allowed_opts $name" in tcl


def test_the_listener_rebuilds_the_command_as_a_list(token):
    """[list] quotes each word, so an object name can never become code."""
    tcl = build_listener_tcl("/tmp/p", token, ALLOWED, OPTIONS)
    assert "set argv [list $verb $object]" in tcl
    assert "uplevel #0 $argv" in tcl
    # The dangerous form: passing a reparsed string.
    assert "uplevel #0 $line" not in tcl
    assert "uplevel #0 $cmd" not in tcl


def test_the_listener_refuses_a_command_name_that_is_not_one(token):
    with pytest.raises(LiveSessionError):
        build_listener_tcl("/tmp/p", token, ["rm -rf /"], OPTIONS)


def test_the_listener_refuses_an_option_name_that_is_not_one(token):
    with pytest.raises(LiveSessionError):
        build_listener_tcl("/tmp/p", token, ALLOWED, ["display"])


def test_a_value_that_cannot_be_embedded_is_refused(token):
    with pytest.raises(LiveSessionError):
        build_listener_tcl("/tmp/p}{", token, ALLOWED, OPTIONS)


# ---------------------------------------------------------------------------
# Client-side validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("obj", ["a\tb", "a\nb", "a\rb", "x{y}", "back\\slash", ""])
def test_a_hostile_object_never_reaches_the_wire(obj):
    with pytest.raises(LiveSessionError):
        validate_object(obj)


def test_a_bus_subscript_is_allowed_because_it_is_data(session, action, tool):
    session.send(action, "/top/u_a/Q[3]")
    assert "/top/u_a/Q[3]" in tool.received[0]


def test_a_bracketed_injection_travels_as_data(session, action, tool):
    """It is sent, but as one field the tool will quote -- never as code."""
    session.send(action, "[exec touch /tmp/pwned]")
    fields = tool.received[0].split("\t")
    assert fields[2] == "[exec touch /tmp/pwned]"


@pytest.mark.parametrize("name,value", [
    ("display", "flat"), ("-display", "a b"), ("-display", "a;b"),
    ("-display", "$x"), ("-1bad", "x"),
])
def test_a_bad_option_is_refused_before_sending(name, value):
    with pytest.raises(LiveSessionError):
        validate_option(name, value)


def test_a_good_option_passes():
    assert validate_option("-display", "flat_schematic") == (
        "-display", "flat_schematic")


# ---------------------------------------------------------------------------
# Talking to the session
# ---------------------------------------------------------------------------
def test_a_permitted_request_reaches_the_tool(session, action, tool):
    session.send(action, "/top/u_a/Q")
    fields = tool.received[0].split("\t")
    assert fields[1] == "add_schematic_objects"
    assert fields[2] == "/top/u_a/Q"
    assert "-display=flat_schematic" in fields
    assert "-highlight=red" in fields


def test_the_token_leads_every_request(session, action, tool, token):
    session.send(action, "/top/u_a/Q")
    assert tool.received[0].split("\t")[0] == token


def test_a_wrong_token_is_refused(tmp_path, tool, action):
    port_file = tmp_path / "control.port"
    port_file.write_text(str(tool.port), encoding="utf-8")
    bad = LiveSession(str(port_file), "0" * 32, timeout=5.0)
    with pytest.raises(LiveSessionError) as excinfo:
        bad.send(action, "/top/u_a/Q")
    assert "bad token" in str(excinfo.value)


def test_a_verb_outside_the_allow_list_is_refused(session, tool):
    with pytest.raises(LiveSessionError) as excinfo:
        session.send(InspectAction("exit"), "x")
    assert "not permitted" in str(excinfo.value)


def test_an_error_reply_becomes_an_exception(tmp_path, token):
    tool = FakeTool(token, reply="ERR may only be used after flattening")
    try:
        port_file = tmp_path / "p"
        port_file.write_text(str(tool.port), encoding="utf-8")
        session = LiveSession(str(port_file), token, timeout=5.0)
        with pytest.raises(LiveSessionError) as excinfo:
            session.send(InspectAction("add_schematic_objects"), "/top/u/Q")
        assert "flattening" in str(excinfo.value)
    finally:
        tool.close()


def test_without_a_port_file_the_message_says_so(tmp_path, action):
    session = LiveSession(str(tmp_path / "missing.port"), "tok")
    with pytest.raises(LiveSessionError) as excinfo:
        session.send(action, "/top/u/Q")
    assert "no control channel" in str(excinfo.value)


def test_a_closed_port_is_reported_not_swallowed(tmp_path, action):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    port_file = tmp_path / "p"
    port_file.write_text(str(port), encoding="utf-8")
    session = LiveSession(str(port_file), "tok", timeout=2.0)
    with pytest.raises(LiveSessionError) as excinfo:
        session.send(action, "/top/u/Q")
    assert str(port) in str(excinfo.value)


def test_is_listening_reflects_reality(session, tool):
    assert session.is_listening()


def test_is_listening_is_false_without_a_port(tmp_path):
    assert not LiveSession(str(tmp_path / "missing"), "tok").is_listening()


@pytest.mark.parametrize("text", ["", "not-a-number", "0", "99999"])
def test_a_junk_port_file_reads_as_no_port(tmp_path, text):
    path = tmp_path / "p"
    path.write_text(text, encoding="utf-8")
    assert read_port(str(path)) is None


# ---------------------------------------------------------------------------
# Actions and the bundle
# ---------------------------------------------------------------------------
def test_an_action_renders_the_equivalent_command(action):
    assert action.rendered("/top/u_a/Q") == (
        "add_schematic_objects {/top/u_a/Q} -display flat_schematic "
        "-highlight red")


def test_an_action_without_a_verb_is_refused():
    with pytest.raises(LiveSessionError):
        InspectAction.from_dict({"options": {}})


def test_an_action_round_trips(action):
    assert InspectAction.from_dict(action.as_dict()).as_dict() == action.as_dict()


PROFILE = {
    "name": "chan",
    "psetup": {"executable": "/bin/echo", "proj": "p/1"},
    "tool": {"executable": "/bin/echo"},
    "commands": {
        "load": [{"key": "faults", "command": "read_faults", "label": "Faults"}],
        "open": "open_visualizer",
        "signal_inspect": [
            {"verb": "add_schematic_objects", "label": "Show",
             "options": {"-display": "flat_schematic"}}],
    },
    "control": {"enabled": True, "allowed_commands": ALLOWED,
                "allowed_options": OPTIONS},
}


@pytest.fixture()
def bundle_parts(tmp_path):
    faults = tmp_path / "f.gz"
    faults.write_text("x", encoding="utf-8")
    profile = ToolProfile.from_dict(PROFILE)
    inputs = VisualizerInputs(proj="p/1", paths={"faults": str(faults)})
    term = TerminalSpec("xterm", "-e", "-title", executable="/usr/bin/xterm")
    return profile, inputs, term


def test_the_bundle_embeds_the_listener_before_the_design_loads(bundle_parts,
                                                                tmp_path):
    profile, inputs, term = bundle_parts
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    text = open(bundle.dofile_path, encoding="utf-8").read()
    assert "control channel" in text
    assert text.index("socket -server") < text.index("read_faults")
    assert bundle.token and bundle.port_file


def test_the_token_file_is_private(bundle_parts, tmp_path):
    import os
    import stat as stat_mod

    profile, inputs, term = bundle_parts
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    token_path = os.path.join(bundle.directory, ls.TOKEN_FILE)
    assert stat_mod.S_IMODE(os.stat(token_path).st_mode) == 0o600


def test_a_stale_port_file_is_removed_before_launch(bundle_parts, tmp_path):
    """Otherwise the GUI would talk to a dead or unrelated session."""
    import os

    profile, inputs, term = bundle_parts
    dest = tmp_path / "b"
    dest.mkdir()
    (dest / ls.PORT_FILE).write_text("12345", encoding="utf-8")
    bundle = write_launch_bundle(profile, inputs, str(dest), term)
    assert not os.path.exists(os.path.join(bundle.directory, ls.PORT_FILE))


def test_a_profile_may_switch_the_channel_off(bundle_parts, tmp_path):
    profile, inputs, term = bundle_parts
    profile.control.enabled = False
    bundle = write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)
    assert not bundle.token and not bundle.port_file
    assert "control channel" not in open(bundle.dofile_path,
                                         encoding="utf-8").read()


def test_the_profile_actions_are_read(bundle_parts):
    profile, _inputs, _term = bundle_parts
    actions = signal_inspect_actions(profile)
    assert [a.verb for a in actions] == ["add_schematic_objects"]
    assert actions[0].options == {"-display": "flat_schematic"}


def test_the_shipped_profile_only_allows_display_commands():
    """The allow-list is the last line of defence; keep it to viewing.

    Checks every profile present, so a site-specific one dropped in beside the
    committed template is held to the same rule.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    profiles = sorted((repo / "profiles").glob("*.json"))
    assert profiles, "at least the example profile must be present"
    for path in profiles:
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = data["control"]["allowed_commands"]
        assert allowed, f"{path.name} must state its allow-list"
        for verb in allowed:
            assert verb.startswith(("add_schematic", "delete_schematic",
                                    "open_visualizer")), f"{path.name}: {verb}"
