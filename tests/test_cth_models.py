"""Free Cth.ai models: catalogue, routing through the Cth.ai wrapper, GUI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from atpg_coverage_debug_agent.agent import cth_models  # noqa: E402
from atpg_coverage_debug_agent.agent.debug_agent import (  # noqa: E402
    AgentConfig,
    DebugAgent,
)

_CATALOG = {
    "models": [
        {"id": "google/gemini-3.8-flash",
         "displayName": "Cth.ai Gemini 3.8 Flash", "context_length": 1048576,
         "auth": {"type": "sso", "scope_group": "llm_access"}},
        {"id": "qwen3.8-27b-bf16", "displayName": "Cth.ai Qwen 3.8 27B BF16",
         "auth": {"scope_group": "llm_access"}},
        {"displayName": "no id"},
    ],
    "providerKeys": {"gcp-gemini": "SECRET-KEY-MUST-NOT-LEAK"},
}


def _exe(path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps(_CATALOG))
    monkeypatch.setenv(cth_models.CATALOG_ENV, str(path))
    return str(path)


@pytest.fixture
def tokens(tmp_path, monkeypatch):
    tok = tmp_path / "tokens"
    monkeypatch.setattr(cth_models, "TOKEN_DIR", tok)
    return tok


@pytest.fixture
def plain_shell(tmp_path, monkeypatch):
    """PATH with a fake 'copilot' that echoes its argv as JSON (no wrapper)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "copilot",
         f"#!{sys.executable}\nimport json, sys\n"
         "print('Model     : cth.ai:' + sys.argv[2])\nprint()\n"
         "print(json.dumps(sys.argv[1:]))\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return bindir


@pytest.fixture
def fake_psetup(tmp_path, monkeypatch):
    """cth_psetup stand-in: logs to stderr, runs -x, always exits 0."""
    path = _exe(tmp_path / "cth_psetup",
                f"#!{sys.executable}\nimport subprocess, sys\n"
                "print('[06:33:28] [INFO ] Entering into workarea',"
                " file=sys.stderr, flush=True)\n"
                "print('stray stdout line', flush=True)\n"
                "subprocess.call([sys.argv[sys.argv.index('-cmd') + 1]])\n"
                "print('[06:33:40] [INFO ] Leaving', flush=True)\n")
    monkeypatch.setattr(cth_models, "SETUP_COMMAND", path)
    return path


# -- catalogue and selection ---------------------------------------------------

def test_catalogue_gives_ids_and_names_but_never_keys(catalog):
    models = cth_models.load_free_models()
    assert [m.model_id for m in models] == ["google/gemini-3.8-flash",
                                            "qwen3.8-27b-bf16"]
    assert models[0].name == "Gemini 3.8 Flash"
    assert models[0].context_length == 1048576
    assert models[0].selection == "cth.ai:google/gemini-3.8-flash"
    assert "SECRET" not in repr(models)
    assert "Preferred" in models[0].tooltip()
    assert "2000 per day" in models[0].tooltip()


def test_missing_or_corrupt_catalogue_is_empty(tmp_path):
    assert cth_models.load_free_models(str(tmp_path / "none.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert cth_models.load_free_models(str(bad)) == []


def test_free_model_id():
    assert cth_models.free_model_id("cth.ai:qwen3.8-27b-bf16") == \
        "qwen3.8-27b-bf16"
    assert cth_models.free_model_id("claude-opus-5") is None
    assert cth_models.free_model_id("cth.ai:") is None
    assert cth_models.free_model_id("") is None


def test_login_status_follows_the_token_file(tokens):
    assert not cth_models.login_done()
    tokens.mkdir()
    (tokens / "llm_access.json").write_text("{}")
    assert cth_models.login_done()


def test_wrapper_detected_only_when_it_supports_cth_model(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "copilot", "#!/bin/sh\necho plain copilot\n")
    assert cth_models.find_wrapper({"PATH": str(bindir)}) is None
    _exe(bindir / "copilot", "#!/usr/bin/env python3\n# --cth.model MODEL_ID\n")
    assert cth_models.find_wrapper({"PATH": str(bindir)}) == \
        str(bindir / "copilot")
    assert cth_models.find_wrapper({"PATH": ""}) is None


# -- command building ----------------------------------------------------------

def test_inside_a_cth_shell_the_wrapper_runs_directly(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = _exe(bindir / "copilot", "#!/bin/sh\n# --cth.model\n")
    argv, via = cth_models.build_command(
        "google/gemini-3.8-flash", ["-p", "hi"], str(tmp_path),
        {"PATH": str(bindir)})
    assert via is False
    assert argv == [wrapper, "--cth.model", "google/gemini-3.8-flash",
                    "-p", "hi"]


def test_outside_a_cth_shell_the_call_goes_through_psetup(
        tmp_path, plain_shell, fake_psetup):
    prompt = "it's $(touch /tmp/never) `x` \"quoted\"\nline two"
    argv, via = cth_models.build_command(
        "qwen3.8-27b-bf16", ["-p", prompt, "-s"], str(tmp_path), None)
    assert via is True
    assert argv[0] == fake_psetup
    assert argv[argv.index("-cfg_ov") + 1].endswith("cth-cfg.txt")
    cfg = open(argv[argv.index("-cfg_ov") + 1]).read()
    assert "cth.ai = 2026.09.p017" in cfg
    script = argv[argv.index("-cmd") + 1]
    # "-x" leaves a Cth.ai shell behind that opens a VS Code window.
    assert "-x" not in argv
    # The script hands copilot the exact arguments, with nothing expanded.
    out = subprocess.run(["sh", script], capture_output=True, text=True).stdout
    lines = out.splitlines()
    assert lines[0] == cth_models.BEGIN_MARKER
    assert json.loads(lines[3]) == ["--cth.model", "qwen3.8-27b-bf16",
                                    "-p", prompt, "-s"]
    assert lines[-1] == cth_models.END_MARKER + "0@@"


def test_own_cfg_ov_file_wins(tmp_path, monkeypatch, plain_shell):
    own = tmp_path / "mine.txt"
    own.write_text("[toolversion]\ncth.ai = 2027.01\n")
    monkeypatch.setenv(cth_models.CFG_OV_ENV, str(own))
    argv = cth_models.psetup_argv("/x.sh")
    assert argv[argv.index("-cfg_ov") + 1] == str(own)


def test_login_command_runs_autobots_login_inside_the_setup(
        plain_shell, fake_psetup):
    argv = cth_models.login_argv()
    assert argv[0] == fake_psetup
    script = open(argv[argv.index("-cmd") + 1]).read()
    assert "autobots login --device-code" in script
    assert fake_psetup in cth_models.login_command_text()


# -- output filtering and errors -----------------------------------------------

def test_filter_drops_setup_output_banner_and_reads_the_real_status():
    f = cth_models.OutputFilter(wait_for_marker=True)
    stream = ("[INFO] Preparing\n[INFO] Entering\n" + cth_models.BEGIN_MARKER
              + "\nModel     : cth.ai:x\n\nThe answer\nline 2\n"
              + cth_models.END_MARKER + "3@@\n[INFO] Leaving\n")
    out = "".join(f.feed(stream[i:i + 7]) for i in range(0, len(stream), 7))
    out += f.finish()
    assert out == "The answer\nline 2\n"
    assert f.rc == 3
    assert "Entering" in f.preamble


def test_filter_without_marker_only_drops_the_model_line():
    f = cth_models.OutputFilter(wait_for_marker=False)
    out = f.feed("Model     : cth.ai:x\n\nHello\n") + f.feed("tail") + f.finish()
    assert out == "Hello\ntail"
    assert f.rc is None


@pytest.mark.parametrize("text,expect", [
    ("Error: no cached token found at ~/.autobots_tokens/llm_access.json. "
     "Run 'autobots login' first.", cth_models.LOGIN_NEEDED),
    ("HTTP 403 Forbidden", "no_proxy"),
    ("429 Too Many Requests", "50 calls per minute"),
    ("Error: model 'x' not found in llm.json.", "no longer offered"),
])
def test_known_failures_are_explained(text, expect):
    assert expect in cth_models.explain_error(text)


def test_unknown_failure_is_not_explained():
    assert cth_models.explain_error("segfault") is None


# -- the agent's CLI call --------------------------------------------------------

def _agent(model: str) -> DebugAgent:
    return DebugAgent(AgentConfig(backend="cli", cli_path="",
                                  cli_model=model, cli_token="ghp_unused"))


def test_agent_runs_a_free_model_through_psetup(plain_shell, fake_psetup):
    chunks = []
    text = _agent("cth.ai:google/gemini-3.8-flash")._call_cli(
        "SYSTEM", "PAYLOAD", on_chunk=chunks.append)
    args = json.loads(text)
    assert args[:2] == ["--cth.model", "google/gemini-3.8-flash"]
    assert "--model" not in args
    assert "-p" in args and "SYSTEM" in args[args.index("-p") + 1]
    joined = "".join(chunks)
    assert "stray" not in joined and "Leaving" not in joined
    assert "Model" not in joined
    assert cth_models.BEGIN_MARKER not in joined


def test_free_model_calls_run_without_a_display(plain_shell, fake_psetup,
                                                monkeypatch):
    # The Cth setup shows a splash screen whenever it can reach a display.
    monkeypatch.setenv("DISPLAY", ":99")
    _exe(plain_shell / "copilot",
         f"#!{sys.executable}\nimport os\n"
         "print(os.environ.get('DISPLAY', 'NO-DISPLAY'))\n")
    out = _agent("cth.ai:qwen3.8-27b-bf16")._call_cli("S", "P")
    assert out == "NO-DISPLAY"
    assert os.environ["DISPLAY"] == ":99"


def test_agent_reports_the_missing_login(plain_shell, fake_psetup):
    _exe(plain_shell / "copilot",
         "#!/bin/sh\necho \"Error: no cached token found at x. "
         "Run 'autobots login' first.\"\nexit 1\n")
    with pytest.raises(cth_models.CthLoginRequired) as err:
        _agent("cth.ai:qwen3.8-27b-bf16")._call_cli("S", "P")
    assert str(err.value) == cth_models.LOGIN_NEEDED


def test_agent_sees_the_inner_failure_although_psetup_exits_0(
        plain_shell, fake_psetup):
    _exe(plain_shell / "copilot", "#!/bin/sh\necho boom\nexit 7\n")
    with pytest.raises(RuntimeError, match="exited 7"):
        _agent("cth.ai:qwen3.8-27b-bf16")._call_cli("S", "P")


def test_free_model_counts_as_configured_without_a_cli_path():
    assert AgentConfig(backend="cli", cli_model="cth.ai:x").configured
    assert not AgentConfig(backend="cli", cli_model="gpt-5").configured


# -- prompts too long for the command line ---------------------------------------

_ECHO_STDIN = (f"#!{sys.executable}\nimport json, sys\n"
               "# --cth.model\n"
               "data = sys.stdin.read()\n"
               "print(json.dumps({'p': '-p' in sys.argv, 'stdin': len(data),"
               " 'tail': data[-20:]}))\n")
_BIG = "x" * 300_000 + " END-OF-PROMPT"


def _sent(text):
    return json.loads(text.strip().splitlines()[-1])


def test_a_long_prompt_is_piped_through_the_cth_setup(plain_shell,
                                                      fake_psetup):
    _exe(plain_shell / "copilot", _ECHO_STDIN.replace("# --cth.model\n", ""))
    got = _sent(_agent("cth.ai:qwen3.8-27b-bf16")._call_cli("S", _BIG))
    assert not got["p"] and got["stdin"] > 300_000


def test_a_long_prompt_is_piped_to_the_wrapper_in_a_cth_shell(plain_shell):
    _exe(plain_shell / "copilot", _ECHO_STDIN)
    got = _sent(_agent("cth.ai:qwen3.8-27b-bf16")._call_cli("S", _BIG))
    assert not got["p"] and got["stdin"] > 300_000


def test_a_long_prompt_is_piped_to_the_copilot_cli(tmp_path):
    exe = _exe(tmp_path / "copilot", _ECHO_STDIN)
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=exe,
                                   cli_model="gpt-5"))
    got = _sent(agent._call_cli("S", _BIG))
    assert not got["p"] and got["stdin"] > 300_000


def test_a_short_prompt_still_uses_the_command_line(tmp_path):
    exe = _exe(tmp_path / "copilot", _ECHO_STDIN)
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=exe,
                                   cli_model="gpt-5"))
    got = _sent(agent._call_cli("S", "short question"))
    assert got["p"] and got["stdin"] == 0


# -- GUI -----------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from atpg_coverage_debug_agent.agent import cli_models  # noqa: E402
from atpg_coverage_debug_agent.gui import agent_panel as ap  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp, catalog, tokens, plain_shell, fake_psetup, monkeypatch):
    monkeypatch.setattr(ap.AgentPanel, "refresh_models", lambda self: None)
    p = ap.AgentPanel()
    p._apply_models([cli_models.ModelInfo("auto"),
                     cli_models.ModelInfo("claude-opus-5")])
    return p


def _items(panel):
    c = panel.cli_model_combo
    return [c.itemText(i) for i in range(c.count())]


def _pick(panel, model_id):
    panel.set_cli_model_selection(cth_models.PREFIX + model_id)


def test_free_group_comes_first_with_headers(panel):
    items = _items(panel)
    assert items[0].startswith("\u2500\u2500 Free")
    assert "slower" in items[0]
    assert items[1:3] == ["Gemini 3.8 Flash  (free)",
                          "Qwen 3.8 27B BF16  (free)"]
    assert "GitHub Copilot" in items[3]
    assert items[4:] == ["auto", "claude-opus-5"]
    model = panel.cli_model_combo.model()
    assert not model.item(0).isEnabled() and not model.item(3).isEnabled()
    assert panel.cli_model_selection() == "auto"


def test_picking_a_free_model_sets_the_config_and_survives_settings(panel):
    _pick(panel, "google/gemini-3.8-flash")
    assert panel.current_config().cli_model == "cth.ai:google/gemini-3.8-flash"
    saved = panel.export_settings()
    assert saved["cli_model"] == "cth.ai:google/gemini-3.8-flash"
    panel.set_cli_model_selection("auto")
    panel.import_settings(saved)
    assert panel.cli_model_combo.currentText() == "Gemini 3.8 Flash  (free)"
    # A Copilot refresh keeps the free selection.
    panel._apply_models([cli_models.ModelInfo("auto"),
                         cli_models.ModelInfo("gpt-5.6-sol")])
    assert panel.cli_model_selection() == "cth.ai:google/gemini-3.8-flash"


def test_note_explains_free_and_slow_and_login(panel):
    assert panel.cli_model_note.isHidden()
    _pick(panel, "qwen3.8-27b-bf16")
    text = panel.cli_model_note.text()
    assert not panel.cli_model_note.isHidden()
    assert "not counted" in text and "Slower here" in text
    assert "One-time login" in text


def test_readiness_shows_the_login_indicator(panel, tokens):
    _pick(panel, "google/gemini-3.8-flash")
    items = {label: (state, detail, action)
             for label, state, detail, action in panel.readiness_items()}
    assert items["Cth.ai login"] == ("fail", "one-time login needed",
                                     "cthai_login")
    assert "Sign-in" not in items
    assert items["Model"][1] == "Gemini 3.8 Flash (free, Cth.ai)"
    panel.refresh_readiness()
    assert panel.readiness_btn.text() == "Log in\u2026"
    tokens.mkdir()
    (tokens / "llm_access.json").write_text("{}")
    panel.refresh_readiness()
    items = {label: state for label, state, _, _ in panel.readiness_items()}
    assert items["Cth.ai login"] == "ok"
    panel._update_model_note()
    assert "One-time login" not in panel.cli_model_note.text()


def test_copilot_models_keep_the_github_sign_in_item(panel):
    labels = [i[0] for i in panel.readiness_items()]
    assert "Sign-in" in labels and "Cth.ai login" not in labels


def test_expired_login_turns_the_indicator_red_until_a_new_login(
        panel, tokens):
    tokens.mkdir()
    tok = tokens / "llm_access.json"
    tok.write_text("{}")
    _pick(panel, "google/gemini-3.8-flash")
    assert panel.cthai_login_ok()
    assert panel._note_cthai_failure("x " + cth_models.LOGIN_NEEDED)
    assert not panel.cthai_login_ok()
    os.utime(tok, (tok.stat().st_atime, tok.stat().st_mtime + 10))
    panel._poll_cthai_login()
    assert panel.cthai_login_ok()
    assert not panel._login_timer.isActive()


def test_log_in_opens_a_terminal_with_the_login(panel, monkeypatch):
    calls = []
    monkeypatch.setattr(ap.terminals, "find_terminal",
                        lambda *a: ap.terminals.TerminalSpec(
                            "xterm", exec_flag="-e", title_flag="-title",
                            executable="/usr/bin/xterm"))
    monkeypatch.setattr(ap.terminals, "display_available", lambda: True)
    monkeypatch.setattr(ap, "_start_detached",
                        lambda argv: calls.append(list(argv)) or True)
    _pick(panel, "google/gemini-3.8-flash")
    panel.refresh_readiness()
    panel._on_readiness_fix()
    assert calls and calls[0][0] == "/usr/bin/xterm"
    assert cth_models.SETUP_COMMAND in calls[0]
    assert panel._login_timer.isActive()
    panel._login_timer.stop()


def test_log_in_without_a_terminal_copies_the_command(panel, monkeypatch):
    monkeypatch.setattr(ap.terminals, "find_terminal", lambda *a: None)
    panel.start_cthai_login()
    assert QApplication.clipboard().text() == cth_models.login_command_text()
    assert "clipboard" in panel.status_label.text()
    panel._login_timer.stop()


def test_without_a_catalogue_the_list_is_unchanged(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv(cth_models.CATALOG_ENV, str(tmp_path / "none.json"))
    monkeypatch.setattr(ap.AgentPanel, "refresh_models", lambda self: None)
    p = ap.AgentPanel()
    p._apply_models([cli_models.ModelInfo("auto"),
                     cli_models.ModelInfo("claude-opus-5")])
    assert _items(p) == ["auto", "claude-opus-5"]
