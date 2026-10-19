"""Reopen the GUI inside Cth.ai without losing the open work."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from atpg_coverage_debug_agent.agent import cth_models  # noqa: E402
from atpg_coverage_debug_agent.app import run_analysis  # noqa: E402
from atpg_coverage_debug_agent.config import settings as settings_mod  # noqa: E402
from atpg_coverage_debug_agent.gui import main_window as mw  # noqa: E402
from atpg_coverage_debug_agent.gui import reopen  # noqa: E402
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel  # noqa: E402


def _exe(path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ward(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return cth_models.ward_dir()


@pytest.fixture
def fake_psetup(tmp_path, monkeypatch):
    path = _exe(tmp_path / "cth_psetup",
                f"#!{sys.executable}\nimport subprocess, sys\n"
                "print('[INFO ] Entering into workarea', flush=True)\n"
                "sys.exit(subprocess.call("
                "[sys.argv[sys.argv.index('-cmd') + 1]]))\n")
    monkeypatch.setattr(cth_models, "SETUP_COMMAND", path)
    return path


@pytest.fixture
def window(qapp, tmp_path, monkeypatch, ward, fake_psetup):
    monkeypatch.setattr(settings_mod, "_DEFAULT_CONFIG_FILE",
                        tmp_path / "settings.json")
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    win = mw.MainWindow()
    closed = []
    monkeypatch.setattr(win, "close", lambda: closed.append(True))
    win.closed = closed
    errors = []
    monkeypatch.setattr(win, "_error", errors.append)
    win.errors = errors
    yield win
    win.agent_panel.shutdown()


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


def _wait(win, limit=20.0):
    end = time.monotonic() + limit
    while getattr(win, "_reopen_state", None) is not None:
        assert time.monotonic() < end, "reopen never finished"
        QApplication.processEvents()
        win._poll_reopen()
        time.sleep(0.05)


# -- the command ---------------------------------------------------------------

def test_reopen_runs_the_gui_inside_the_setup_with_the_snapshot(
        tmp_path, ward, fake_psetup):
    fake_py = _exe(tmp_path / "py",
                   "#!/bin/sh\necho \"$PWD|$PYTHONPATH|$COPILOT_HOME|$*\"\n")
    argv = cth_models.reopen_argv("/snap dir/manifest.json", fake_py,
                                  str(tmp_path), "/cp home")
    assert argv[0] == fake_psetup
    assert "-cmd" in argv and "-x" not in argv
    out = subprocess.run(argv, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": "/old"}).stdout
    line = out.strip().splitlines()[-1]
    assert line == (f"{tmp_path}|{tmp_path}:/old|/cp home|"
                    "-m atpg_coverage_debug_agent --restore "
                    "/snap dir/manifest.json")


# -- the snapshot --------------------------------------------------------------

def test_snapshot_keeps_reports_partitions_and_the_investigation(
        tmp_path, demo_report):
    edited = run_analysis(*mw._demo_inputs())
    edited.investigation = {"diagnosis": "D", "chat": [
        {"role": "You", "text": "Q"}], "trace": "", "findings": [],
        "fix_plan_edits": []}
    parts = [{"name": "a", "report": edited, "base_report": demo_report},
             {"name": "b", "report": demo_report,
              "base_report": demo_report}]
    manifest = reopen.write_snapshot(tmp_path, parts, 1, None, None,
                                     "Triage")
    snap = reopen.read_snapshot(str(manifest))
    assert snap["multi"] and snap["active"] == 1 and snap["tab"] == "Triage"
    got = snap["partitions"]
    assert [p["name"] for p in got] == ["a", "b"]
    assert got[0]["report"].investigation["chat"][0]["text"] == "Q"
    assert got[0]["base_report"] is not got[0]["report"]
    assert got[1]["base_report"] is got[1]["report"]
    assert oct(manifest.parent.stat().st_mode & 0o777) == "0o700"
    reopen.discard(manifest)
    assert not manifest.parent.exists()


# -- the window ----------------------------------------------------------------

def _pick_free(win):
    panel = win.agent_panel
    panel._free_models = [cth_models.FreeModel("qwen3.8-27b-bf16", "Qwen")]
    panel._fill_model_combo("cth.ai:qwen3.8-27b-bf16")
    panel._update_model_note()


def test_button_only_when_a_free_model_is_slow(window):
    panel = window.agent_panel
    panel._cth_wrapper = None
    _pick_free(window)
    assert not panel.cthai_reopen_btn.isHidden()
    panel._cth_wrapper = "/cth/copilot"
    panel._update_model_note()
    assert panel.cthai_reopen_btn.isHidden()
    panel._cth_wrapper = None
    panel.set_cli_model_selection("auto")
    assert panel.cthai_reopen_btn.isHidden()


def test_a_running_job_blocks_the_reopen(window, monkeypatch):
    shown, started = [], []
    monkeypatch.setattr(window.agent_panel, "is_busy", lambda: True)
    monkeypatch.setattr(mw.QMessageBox, "information",
                        lambda *a: shown.append(a[2]))
    monkeypatch.setattr(window, "_start_reopen", lambda: started.append(1))
    window.on_reopen_in_cthai()
    assert "still going" in shown[0] and not started


def test_cancel_in_the_warning_keeps_everything(window, monkeypatch):
    started, texts = [], []

    def fake_exec(box):
        texts.append(box.informativeText())
        return 0
    monkeypatch.setattr(mw.QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(window, "_start_reopen", lambda: started.append(1))
    window.on_reopen_in_cthai()
    assert not started
    assert "Not carried over: undo history" in texts[0]


def test_old_window_closes_only_when_the_new_one_is_ready(
        window, tmp_path, monkeypatch, demo_report, ward):
    _show(window, demo_report)
    window.agent_panel._chat_turns = [("You", "why?")]
    seen = tmp_path / "seen.json"
    fake_py = _exe(tmp_path / "py",
                   f"#!{sys.executable}\nimport json, os, sys, time\n"
                   "m = sys.argv[sys.argv.index('--restore') + 1]\n"
                   "time.sleep(0.5)\n"
                   f"open({str(seen)!r}, 'w').write(open(m).read())\n"
                   "open(os.path.join(os.path.dirname(m), 'ready'), 'w')"
                   ".close()\n")
    monkeypatch.setattr(sys, "executable", fake_py)
    window._start_reopen()
    state = window._reopen_state
    assert not window.closed
    _wait(window)
    assert window.closed == [True] and not window.errors
    assert not state["manifest"].parent.exists()
    manifest = json.loads(seen.read_text())
    assert manifest["reports"][0]["report"] == "report_0.json"


def test_a_failed_start_keeps_the_old_window(window, tmp_path, monkeypatch,
                                             demo_report):
    _show(window, demo_report)
    fake_py = _exe(tmp_path / "py", "#!/bin/sh\necho no display >&2\nexit 3\n")
    monkeypatch.setattr(sys, "executable", fake_py)
    window._start_reopen()
    manifest = window._reopen_state["manifest"]
    _wait(window)
    assert not window.closed
    assert "exit status 3" in window.errors[0]
    assert "Nothing was lost" in window.errors[0]
    assert "no display" in window.errors[0]
    assert not manifest.parent.exists()


def test_cancel_while_waiting_stops_the_new_window(window, tmp_path,
                                                   monkeypatch):
    fake_py = _exe(tmp_path / "py", "#!/bin/sh\nexec sleep 30\n")
    monkeypatch.setattr(sys, "executable", fake_py)
    window._start_reopen()
    state = window._reopen_state
    window._cancel_reopen()
    assert window._reopen_state is None
    assert state["proc"].wait(10) != 0
    assert not window.closed
    assert not state["manifest"].parent.exists()


def test_new_window_restores_the_work_and_tells_the_old_one(
        window, qapp, tmp_path, monkeypatch, demo_report):
    report = run_analysis(*mw._demo_inputs())
    report.investigation = {"diagnosis": "Root cause: X", "chat": [
        {"role": "You", "text": "why?"}], "trace": "t", "findings": [],
        "fix_plan_edits": []}
    manifest = reopen.write_snapshot(tmp_path, [], -1, report, demo_report,
                                     "AI Debug Agent")
    assert window.restore_snapshot(str(manifest))
    assert reopen.is_ready(manifest)
    assert window._report.summary.coverage_loss_count == \
        report.summary.coverage_loss_count
    assert window._base_report is not window._report
    assert window.agent_panel._chat_turns[0][1] == "why?"
    assert window.tabs.tabText(window.tabs.currentIndex()) == "AI Debug Agent"


def test_new_window_that_cannot_load_reports_it_and_closes(window, tmp_path):
    folder = tmp_path / "snap"
    folder.mkdir()
    manifest = folder / reopen.MANIFEST
    manifest.write_text("{broken")
    assert not window.restore_snapshot(str(manifest))
    assert reopen.failure(manifest)
    assert not reopen.is_ready(manifest)
    QApplication.processEvents()
    assert window.closed == [True]


def test_any_restore_error_is_reported_to_the_old_window(window, tmp_path,
                                                         monkeypatch,
                                                         demo_report):
    manifest = reopen.write_snapshot(tmp_path, [], -1, demo_report,
                                     demo_report, "")

    def boom(snap):
        raise AttributeError("no such field")
    monkeypatch.setattr(window, "_restore_work", boom)
    assert not window.restore_snapshot(str(manifest))
    assert "AttributeError: no such field" in reopen.failure(manifest)


def test_once_the_new_window_is_up_the_old_one_waits_for_the_load(
        window, tmp_path, monkeypatch, demo_report):
    fake_py = _exe(tmp_path / "py",
                   f"#!{sys.executable}\nimport os, sys, time\n"
                   "m = sys.argv[sys.argv.index('--restore') + 1]\n"
                   "open(os.path.join(os.path.dirname(m), 'started'), 'w')"
                   ".close()\n"
                   "time.sleep(1.5)\n"
                   "open(os.path.join(os.path.dirname(m), 'ready'), 'w')"
                   ".close()\n")
    monkeypatch.setattr(sys, "executable", fake_py)
    monkeypatch.setattr(reopen, "TIMEOUT_S", 0)
    _show(window, demo_report)
    window._start_reopen()
    state = window._reopen_state
    end = time.monotonic() + 20
    while not reopen.is_started(state["manifest"]):
        assert time.monotonic() < end
        time.sleep(0.05)
    window._poll_reopen()
    assert window._reopen_state is state, "a started window is not timed out"
    assert "loading your work" in state["dialog"].labelText()
    _wait(window)
    assert window.closed == [True] and not window.errors


# -- the running Tessent Visualizer session ------------------------------------

def _fake_session_dir(tmp_path, token="tok123", port="40123"):
    from atpg_coverage_debug_agent.launcher.live_session import (
        PORT_FILE, TOKEN_FILE)
    d = tmp_path / "vis_bundle"
    d.mkdir()
    if token is not None:
        (d / TOKEN_FILE).write_text(token)
    (d / PORT_FILE).write_text(port)
    return {"directory": str(d), "port_file": str(d / PORT_FILE),
            "log_path": "", "heavy": False, "agent_eval": True,
            "job_file": "", "launch_form": {"profile": "ttlc"}}


def test_the_session_location_travels_without_its_token(tmp_path,
                                                        demo_report):
    vis = _fake_session_dir(tmp_path)
    manifest = reopen.write_snapshot(tmp_path, [], -1, demo_report,
                                     demo_report, "", visualizer=vis)
    assert "tok123" not in manifest.read_text()
    assert reopen.read_snapshot(str(manifest))["visualizer"] == vis


def test_reopen_hands_over_the_live_session(window, tmp_path, monkeypatch,
                                            demo_report):
    vis = _fake_session_dir(tmp_path)
    monkeypatch.setattr(window.visualizer_panel, "export_live_session",
                        lambda: vis)
    seen = {}
    real = reopen.write_snapshot

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(reopen, "write_snapshot", spy)
    monkeypatch.setattr(sys, "executable", "/bin/true")
    _show(window, demo_report)
    window._start_reopen()
    window._cancel_reopen()
    assert seen["visualizer"] == vis


def test_the_new_window_reattaches_to_the_running_session(
        window, tmp_path, demo_report):
    vis = _fake_session_dir(tmp_path)
    manifest = reopen.write_snapshot(tmp_path, [], -1, demo_report,
                                     demo_report, "", visualizer=vis)
    panel = window.visualizer_panel
    assert not panel.has_live_session()
    assert window.restore_snapshot(str(manifest))
    panel._poll_session()
    assert panel.has_live_session()
    assert "port 40123" in panel.session_label.text()
    target = panel.agent_target()
    assert target["agent_eval"] and target["profile"] == "ttlc"
    assert panel.export_live_session()["port_file"] == vis["port_file"]


def test_a_session_without_its_token_is_not_reattached(window, tmp_path,
                                                       demo_report):
    vis = _fake_session_dir(tmp_path, token=None)
    manifest = reopen.write_snapshot(tmp_path, [], -1, demo_report,
                                     demo_report, "", visualizer=vis)
    assert window.restore_snapshot(str(manifest))
    assert window.visualizer_panel.agent_target() is None
