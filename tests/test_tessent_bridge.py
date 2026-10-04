"""Agent-driven Tessent commands: listener, bridge, MCP tools and approval UI.

What matters most is what does NOT happen: a script never reaches the tool
without the GUI approving it, and the tool server never holds the token.
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import threading
import time

import pytest

from atpg_coverage_debug_agent import mcp_server
from atpg_coverage_debug_agent.launcher import agent_bridge as ab
from atpg_coverage_debug_agent.launcher import live_session as ls
from atpg_coverage_debug_agent.launcher.live_session import (
    LiveSession, LiveSessionError, ScriptResult, build_listener_tcl,
    new_token, slice_transcript, validate_script,
)
from atpg_coverage_debug_agent.launcher.profiles import ToolProfile
from atpg_coverage_debug_agent.launcher.terminals import TerminalSpec
from atpg_coverage_debug_agent.launcher.visualizer import (
    VisualizerInputs, write_launch_bundle,
)


# ---------------------------------------------------------------------------
# The generated Tcl
# ---------------------------------------------------------------------------
def _tcl(allow_eval):
    return build_listener_tcl("/tmp/p", new_token(), ["add_schematic_objects"],
                              ["-display"], allow_eval=allow_eval)


def test_agent_scripts_are_off_unless_the_profile_enables_them():
    assert "set ::atpg_allow_eval 0" in _tcl(False)
    assert "set ::atpg_allow_eval 1" in _tcl(True)
    assert "agent scripts are disabled in this profile" in _tcl(True)


def test_the_token_is_checked_before_an_agent_script_is_dispatched():
    tcl = _tcl(True)
    serve = tcl[tcl.index("proc ::atpg_serve"):]
    assert serve.index("bad token") < serve.index("::atpg_eval $chan")


def test_the_script_travels_encoded_and_is_bracketed_by_markers():
    tcl = _tcl(True)
    assert "binary decode base64" in tcl
    assert 'puts "ATPG_BEGIN $id"' in tcl
    assert 'puts "ATPG_END $id rc=$rc"' in tcl
    assert ls.EVAL_VERB in tcl
    # The structured inspect path is unchanged.
    assert "uplevel #0 $argv" in tcl
    assert "uplevel #0 $line" not in tcl


def test_the_eval_verb_cannot_be_put_on_an_allow_list():
    with pytest.raises(LiveSessionError):
        build_listener_tcl("/tmp/p", new_token(), [ls.EVAL_VERB], [])


# ---------------------------------------------------------------------------
# Transcript slicing
# ---------------------------------------------------------------------------
def test_the_transcript_between_the_markers_is_returned(tmp_path):
    log = tmp_path / "t.log"
    log.write_text("noise\nATPG_BEGIN abc\nline 1\nline 2\nATPG_END abc rc=0\n"
                   "later\n", encoding="utf-8")
    assert slice_transcript(str(log), "abc", wait=0) == ("line 1\nline 2", True)


def test_another_requests_markers_are_ignored(tmp_path):
    log = tmp_path / "t.log"
    log.write_text("ATPG_BEGIN zzz\nother\nATPG_END zzz rc=0\n",
                   encoding="utf-8")
    assert slice_transcript(str(log), "abc", wait=0) == ("", False)


def test_a_missing_end_marker_is_reported_incomplete(tmp_path):
    log = tmp_path / "t.log"
    log.write_text("ATPG_BEGIN abc\npartial\n", encoding="utf-8")
    assert slice_transcript(str(log), "abc", wait=0) == ("partial", False)


def test_reading_starts_at_the_offset(tmp_path):
    log = tmp_path / "t.log"
    old = "ATPG_BEGIN abc\nstale\nATPG_END abc rc=0\n"
    log.write_text(old + "ATPG_BEGIN abc\nfresh\nATPG_END abc rc=0\n",
                   encoding="utf-8")
    assert slice_transcript(str(log), "abc", offset=len(old), wait=0) == (
        "fresh", True)


@pytest.mark.parametrize("script", ["", "   ", "a\0b", "x" * (ls.MAX_SCRIPT_CHARS + 1)])
def test_unusable_scripts_are_refused(script):
    with pytest.raises(LiveSessionError):
        validate_script(script)


# ---------------------------------------------------------------------------
# run_script against a stand-in for the tool
# ---------------------------------------------------------------------------
class FakeEvalTool:
    """Speaks the EVAL contract: decodes, logs markers, replies encoded."""

    def __init__(self, token, log_path, enabled=True, fail=False):
        self.token = token
        self.log_path = log_path
        self.enabled = enabled
        self.fail = fail
        self.scripts = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                parts = buf.decode().strip().split("\t")
                if parts[0] != self.token:
                    conn.sendall(b"ERR bad token\n")
                    continue
                if parts[1] != ls.EVAL_VERB or not self.enabled:
                    conn.sendall(b"ERR agent scripts are disabled in this profile\n")
                    continue
                req_id = parts[2]
                script = base64.b64decode(parts[3]).decode()
                self.scripts.append(script)
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"ATPG_BEGIN {req_id}\nprinted: {script}\n"
                             f"ATPG_END {req_id} rc={1 if self.fail else 0}\n")
                status = "ERR64" if self.fail else "OK64"
                result = base64.b64encode(b"returned\nvalue").decode()
                conn.sendall(f"{status} {result}\n".encode())

    def close(self):
        self._sock.close()


@pytest.fixture()
def eval_tool(tmp_path):
    token = new_token()
    log = tmp_path / "tool.log"
    log.write_text("startup noise\n", encoding="utf-8")
    tool = FakeEvalTool(token, str(log))
    port_file = tmp_path / "control.port"
    port_file.write_text(str(tool.port), encoding="utf-8")
    yield tool, LiveSession(str(port_file), token), str(log)
    tool.close()


def test_a_script_runs_and_its_output_comes_back(eval_tool):
    tool, session, log = eval_tool
    res = session.run_script("report_statistics\nputs [info patchlevel]",
                             "abc123", log)
    assert res.ok is True
    assert res.result == "returned\nvalue"
    assert res.transcript.startswith("printed: report_statistics")
    assert res.transcript_complete is True
    assert tool.scripts == ["report_statistics\nputs [info patchlevel]"]


def test_a_failing_script_is_a_result_not_an_exception(eval_tool):
    tool, session, log = eval_tool
    tool.fail = True
    res = session.run_script("bogus_cmd", "abc124", log)
    assert res.ok is False and res.result == "returned\nvalue"


def test_a_disabled_profile_refusal_raises(eval_tool):
    tool, session, log = eval_tool
    tool.enabled = False
    with pytest.raises(LiveSessionError, match="disabled"):
        session.run_script("report_statistics", "abc125", log)


def test_a_bad_request_id_never_reaches_the_wire(eval_tool):
    tool, session, log = eval_tool
    with pytest.raises(LiveSessionError):
        session.run_script("x", "bad id\t", log)
    assert tool.scripts == []


# ---------------------------------------------------------------------------
# The launch bundle
# ---------------------------------------------------------------------------
def _profile(allow_eval, commands=("add_schematic_objects",)):
    return ToolProfile.from_dict({
        "name": "p",
        "psetup": {"executable": "/bin/echo", "proj": "p/1"},
        "tool": {"executable": "/bin/echo"},
        "commands": {"load": [{"key": "faults", "command": "read_faults",
                               "label": "Faults"}],
                     "open": "open_visualizer"},
        "control": {"enabled": True, "allowed_commands": list(commands),
                    "allow_agent_eval": allow_eval},
    })


def _bundle(tmp_path, profile):
    faults = tmp_path / "f.gz"
    faults.write_text("x", encoding="utf-8")
    inputs = VisualizerInputs(proj="p/1", paths={"faults": str(faults)})
    term = TerminalSpec("xterm", "-e", "-title", executable="/usr/bin/xterm")
    return write_launch_bundle(profile, inputs, str(tmp_path / "b"), term)


def test_a_bundle_records_whether_agent_scripts_are_allowed(tmp_path):
    on = _bundle(tmp_path, _profile(True))
    assert on.agent_eval is True
    assert "set ::atpg_allow_eval 1" in open(on.dofile_path).read()
    off = _bundle(tmp_path, _profile(False))
    assert off.agent_eval is False
    assert "set ::atpg_allow_eval 0" in open(off.dofile_path).read()


def test_agent_scripts_alone_still_open_the_channel(tmp_path):
    bundle = _bundle(tmp_path, _profile(True, commands=()))
    assert bundle.token and bundle.agent_eval


def test_the_shipped_profile_round_trips_the_flag():
    spec = _profile(True).control
    assert spec.as_dict()["allow_agent_eval"] is True


# ---------------------------------------------------------------------------
# The bridge files
# ---------------------------------------------------------------------------
def test_a_request_is_pending_until_answered(tmp_path):
    bridge = ab.bridge_dir(str(tmp_path))
    req = ab.submit(bridge, ab.KIND_RUN, "report_x", "because")
    assert [r.id for r in ab.pending(bridge)] == [req.id]
    ab.respond(bridge, req.id, {"status": "rejected"})
    assert ab.pending(bridge) == []
    assert ab.response(bridge, req.id)["status"] == "rejected"


def test_the_bridge_dir_is_private(tmp_path):
    bridge = ab.bridge_dir(str(tmp_path))
    assert os.stat(bridge).st_mode & 0o077 == 0


def test_an_unanswered_request_is_withdrawn(tmp_path):
    bridge = ab.bridge_dir(str(tmp_path))
    req = ab.submit(bridge, ab.KIND_RUN, "report_x")
    data = ab.wait_for_response(bridge, req.id, timeout=0.3, poll=0.05)
    assert data["status"] == "timeout"
    assert ab.is_withdrawn(bridge, req.id)
    assert ab.pending(bridge) == []


def test_a_run_request_needs_a_script(tmp_path):
    bridge = ab.bridge_dir(str(tmp_path))
    with pytest.raises(ab.BridgeError):
        ab.submit(bridge, ab.KIND_RUN, "  ")


@pytest.mark.parametrize("bad", ["../x", "abc", "ABCDEF123456", ""])
def test_request_ids_are_checked(tmp_path, bad):
    with pytest.raises(ab.BridgeError):
        ab.respond(str(tmp_path), bad, {})


def test_without_a_session_dir_the_tools_say_so():
    data = ab.call_tool("tessent_run", {"script": "x"}, "")
    assert data["status"] == "unavailable"


def test_a_long_transcript_is_capped_and_spilled(tmp_path):
    text = "x" * (ab.MAX_INLINE_TRANSCRIPT + 100)
    spill = tmp_path / "t.txt"
    capped = ab.cap_transcript(text, str(spill))
    assert capped["transcript_truncated"] is True
    assert len(capped["transcript"]) < len(text)
    assert spill.read_text() == text


# ---------------------------------------------------------------------------
# The MCP server side
# ---------------------------------------------------------------------------
def _answer_when_asked(bridge, payload, delay=0.1):
    def _run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reqs = ab.pending(bridge)
            if reqs:
                time.sleep(delay)
                ab.respond(bridge, reqs[0].id, payload)
                return
            time.sleep(0.02)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_the_tessent_tools_are_listed():
    names = {t["name"] for t in mcp_server.build_tools_list()}
    assert {"tessent_session_status", "tessent_run"} <= names


def test_tessent_run_waits_for_the_gui_and_returns_its_answer(tmp_path):
    bridge = ab.bridge_dir(str(tmp_path))
    _answer_when_asked(bridge, {"status": "completed", "ok": True,
                                "result": "42", "script_ran": "expr 6*7"})
    state = {"work_dir": str(tmp_path), "tool_log_path": ""}
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "tessent_run",
                   "arguments": {"script": "expr 6*7", "reason": "test"}}},
        state)
    data = json.loads(resp["result"]["content"][0]["text"])
    assert data["status"] == "completed" and data["result"] == "42"
    req = json.load(open(next(
        os.path.join(bridge, n) for n in os.listdir(bridge)
        if n.startswith("req_"))))
    assert req["script"] == "expr 6*7" and req["reason"] == "test"


def test_the_server_never_sees_the_session_token(tmp_path):
    bridge = ab.bridge_dir(str(tmp_path))
    _answer_when_asked(bridge, {"status": "ok", "live": True})
    state = {"work_dir": str(tmp_path), "tool_log_path": ""}
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "tessent_session_status", "arguments": {}}}, state)
    assert "token" not in resp["result"]["content"][0]["text"]
    for name in os.listdir(bridge):
        assert "token" not in open(os.path.join(bridge, name)).read()


def test_progress_is_reported_while_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "PROGRESS_INTERVAL", 0.05)
    bridge = ab.bridge_dir(str(tmp_path))
    _answer_when_asked(bridge, {"status": "rejected"}, delay=0.5)
    out = io.StringIO()
    state = {"work_dir": str(tmp_path), "tool_log_path": "",
             "notify": lambda m: out.write(json.dumps(m) + "\n")}
    mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "tessent_run", "arguments": {"script": "x"},
                   "_meta": {"progressToken": "tok"}}}, state)
    notes = [json.loads(l) for l in out.getvalue().splitlines()]
    assert notes and notes[0]["method"] == "notifications/progress"
    assert notes[0]["params"]["progressToken"] == "tok"


# ---------------------------------------------------------------------------
# The GUI approval box
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeSession:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def run_script(self, script, request_id, log_path=""):
        self.calls.append((script, request_id, log_path))
        if self.fail:
            raise LiveSessionError("the session could not be reached")
        return ScriptResult(ok=True, result="R", transcript="T",
                            transcript_complete=True)


@pytest.fixture()
def gui(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from atpg_coverage_debug_agent.agent.debug_agent import McpSession
    from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel

    # Pumping events would wake the CLI model fetch of every panel other test
    # modules left behind, so the script worker runs inline instead.
    monkeypatch.setattr(AgentPanel, "_start_tessent_worker",
                        staticmethod(lambda fn: fn()))
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    app = QApplication.instance() or QApplication([])
    panel = AgentPanel()
    panel._mcp_session = McpSession(work_dir=str(tmp_path), config_path="",
                                    evidence_path="")
    fake = FakeSession()
    target = {"session": fake, "live": True, "agent_eval": True,
              "log_path": "/tmp/log", "profile": "ttlc",
              "design_inputs": {"faults": "/x/f.gz"}}
    holder = {"target": target}
    panel.set_tessent_provider(lambda: holder["target"])
    yield app, panel, fake, holder, ab.bridge_dir(str(tmp_path))
    panel._mcp_session = None
    panel.shutdown()


def _wait_done(app, panel, timeout=5.0):
    assert panel._bridge_running is None


def test_nothing_runs_until_approved(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "report_statistics", "need counts")
    panel._poll_bridge()
    assert panel.pending_tessent_request().id == req.id
    assert panel.tessent_box.isVisibleTo(panel)
    assert "need counts" in panel.tessent_reason_label.text()
    assert fake.calls == []
    assert ab.response(bridge, req.id) is None


def test_approve_runs_it_and_answers_the_agent(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "report_statistics", "why")
    panel._poll_bridge()
    panel.approve_tessent()
    _wait_done(app, panel)
    assert fake.calls == [("report_statistics", req.id, "/tmp/log")]
    resp = ab.response(bridge, req.id)
    assert resp["status"] == "completed" and resp["result"] == "R"
    assert resp["transcript"] == "T" and resp["edited"] is False
    assert not panel.tessent_box.isVisibleTo(panel)
    assert any(role == "Tessent" and "R" in text
               for role, text in panel._chat_turns)


def test_an_edited_script_is_what_runs_and_the_agent_is_told(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "report_everything", "why")
    panel._poll_bridge()
    panel.tessent_script_edit.setPlainText("report_statistics")
    panel.approve_tessent()
    _wait_done(app, panel)
    resp = ab.response(bridge, req.id)
    assert fake.calls[0][0] == "report_statistics"
    assert resp["edited"] is True and resp["script_ran"] == "report_statistics"
    assert resp["original_script"] == "report_everything"


def test_reject_never_runs_it(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "delete_everything", "why")
    panel._poll_bridge()
    panel.reject_tessent()
    assert fake.calls == []
    assert ab.response(bridge, req.id)["status"] == "rejected"


def test_requests_queue_one_at_a_time(gui):
    app, panel, fake, _holder, bridge = gui
    first = ab.submit(bridge, ab.KIND_RUN, "a", "")
    time.sleep(0.01)
    second = ab.submit(bridge, ab.KIND_RUN, "b", "")
    panel._poll_bridge()
    assert panel.pending_tessent_request().id == first.id
    assert "1 more" in panel.tessent_queue_label.text()
    panel.reject_tessent()
    assert panel.pending_tessent_request().id == second.id


def test_without_a_session_the_request_is_refused_at_once(gui):
    app, panel, fake, holder, bridge = gui
    holder["target"] = None
    req = ab.submit(bridge, ab.KIND_RUN, "x", "")
    panel._poll_bridge()
    assert ab.response(bridge, req.id)["status"] == "no_session"
    assert panel.pending_tessent_request() is None


def test_a_profile_without_agent_scripts_refuses(gui):
    app, panel, fake, holder, bridge = gui
    holder["target"] = dict(holder["target"], agent_eval=False)
    req = ab.submit(bridge, ab.KIND_RUN, "x", "")
    panel._poll_bridge()
    assert ab.response(bridge, req.id)["status"] == "disabled"


def test_status_is_answered_without_approval(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_STATUS)
    panel._poll_bridge()
    resp = ab.response(bridge, req.id)
    assert resp["status"] == "ok" and resp["accepts_agent_commands"] is True
    assert resp["design_inputs"] == {"faults": "/x/f.gz"}
    assert "session" not in resp
    assert panel.pending_tessent_request() is None


def test_stop_rejects_what_is_waiting(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "x", "")
    panel._poll_bridge()
    panel._reject_all_tessent("The user stopped the turn.", "cancelled")
    assert ab.response(bridge, req.id)["status"] == "cancelled"
    assert not panel.tessent_box.isVisibleTo(panel)


def test_a_withdrawn_request_leaves_the_screen(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "x", "")
    panel._poll_bridge()
    ab.withdraw(bridge, req.id)
    panel._poll_bridge()
    assert panel.pending_tessent_request() is None


def test_a_channel_failure_reaches_the_agent(gui):
    app, panel, fake, _holder, bridge = gui
    fake.fail = True
    req = ab.submit(bridge, ab.KIND_RUN, "x", "")
    panel._poll_bridge()
    panel.approve_tessent()
    _wait_done(app, panel)
    resp = ab.response(bridge, req.id)
    assert resp["status"] == "error" and "could not be reached" in resp["message"]


def test_allow_all_runs_every_request_without_asking(gui):
    app, panel, fake, _holder, bridge = gui
    assert not panel.tessent_allow_all_check.isChecked()
    panel.tessent_allow_all_check.setChecked(True)
    first = ab.submit(bridge, ab.KIND_RUN, "a", "")
    time.sleep(0.01)
    second = ab.submit(bridge, ab.KIND_RUN, "b", "")
    panel._poll_bridge()
    assert [c[0] for c in fake.calls] == ["a", "b"]
    for req in (first, second):
        resp = ab.response(bridge, req.id)
        assert resp["status"] == "completed" and resp["auto_approved"] is True
    assert panel.pending_tessent_request() is None


def test_turning_allow_all_on_runs_the_waiting_request(gui):
    app, panel, fake, _holder, bridge = gui
    req = ab.submit(bridge, ab.KIND_RUN, "a", "")
    panel._poll_bridge()
    assert fake.calls == []
    panel.tessent_allow_all_check.setChecked(True)
    assert ab.response(bridge, req.id)["status"] == "completed"


def test_turning_allow_all_off_asks_again(gui):
    app, panel, fake, _holder, bridge = gui
    panel.tessent_allow_all_check.setChecked(True)
    panel.tessent_allow_all_check.setChecked(False)
    req = ab.submit(bridge, ab.KIND_RUN, "a", "")
    panel._poll_bridge()
    assert fake.calls == []
    assert panel.pending_tessent_request().id == req.id


def test_allow_all_still_refuses_without_a_session(gui):
    app, panel, fake, holder, bridge = gui
    holder["target"] = None
    panel.tessent_allow_all_check.setChecked(True)
    req = ab.submit(bridge, ab.KIND_RUN, "a", "")
    panel._poll_bridge()
    assert ab.response(bridge, req.id)["status"] == "no_session"
