"""Tests for the live Copilot CLI model-list discovery."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from atpg_coverage_debug_agent.agent import cli_models

_MODELS = [
    {"modelId": "auto", "name": "Auto", "description": "Let Copilot pick"},
    {"modelId": "claude-opus-5", "name": "Claude Opus 5",
     "description": "Claude Opus 5"},
    {"modelId": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
    {"name": "no id at all"},          # must be skipped
]


def _fake_cli(tmp_path, body: str):
    """Write an executable stub that answers the ACP handshake."""
    script = tmp_path / "copilot"
    script.write_text(f"#!{sys.executable}\nimport json, sys\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


_ANSWER_OK = f"""
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg["id"] == 1:
        print(json.dumps({{"jsonrpc": "2.0", "id": 1, "result": {{}}}}),
              flush=True)
    elif msg["id"] == 2:
        print(json.dumps({{"jsonrpc": "2.0", "id": 2, "result": {{
            "sessionId": "x",
            "models": {{"availableModels": {json.dumps(_MODELS)}}}}}}}),
              flush=True)
        break
"""

_ANSWER_ERROR = """
for line in sys.stdin:
    msg = json.loads(line)
    if msg["id"] == 2:
        print(json.dumps({"jsonrpc": "2.0", "id": 2,
                          "error": {"code": -32000,
                                    "message": "not authenticated"}}),
              flush=True)
        break
"""

_ANSWER_NOISE = f"""
print("qt.qpa.xcb: not protocol output", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg["id"] == 2:
        print(json.dumps({{"jsonrpc": "2.0", "id": 2, "result": {{
            "models": {{"availableModels": {json.dumps(_MODELS)}}}}}}}),
              flush=True)
        break
"""


def test_fetch_parses_available_models(tmp_path):
    models = cli_models.fetch_models(_fake_cli(tmp_path, _ANSWER_OK))
    assert [m.model_id for m in models] == ["auto", "claude-opus-5",
                                            "gpt-5.6-sol"]
    assert models[1].name == "Claude Opus 5"


def test_fetch_survives_non_protocol_output(tmp_path):
    models = cli_models.fetch_models(_fake_cli(tmp_path, _ANSWER_NOISE))
    assert "claude-opus-5" in [m.model_id for m in models]


def test_fetch_returns_empty_on_agent_error(tmp_path):
    assert cli_models.fetch_models(_fake_cli(tmp_path, _ANSWER_ERROR)) == []


def test_fetch_returns_empty_for_a_missing_cli(tmp_path):
    assert cli_models.fetch_models(str(tmp_path / "nope")) == []
    assert cli_models.fetch_models("") == []


def test_fetch_gives_up_on_a_silent_cli(tmp_path):
    silent = _fake_cli(tmp_path, "import time; time.sleep(30)")
    assert cli_models.fetch_models(silent, timeout=2.0) == []


def test_model_ids_puts_auto_first(tmp_path):
    models = [cli_models.ModelInfo("gpt-5.6-sol"),
              cli_models.ModelInfo("auto"),
              cli_models.ModelInfo("claude-opus-5")]
    assert cli_models.model_ids(models) == ["auto", "gpt-5.6-sol",
                                            "claude-opus-5"]
    # 'auto' is offered even when the CLI did not advertise it.
    assert cli_models.model_ids([cli_models.ModelInfo("x")]) == ["auto", "x"]


def test_cache_round_trip(tmp_path):
    cache = tmp_path / "cli_models.json"
    models = [cli_models.ModelInfo("auto", "Auto"),
              cli_models.ModelInfo("claude-opus-5", "Claude Opus 5")]
    cli_models.save_cached_models(models, cache)
    assert cli_models.load_cached_models(cache) == models


def test_cache_missing_or_corrupt_is_empty(tmp_path):
    cache = tmp_path / "cli_models.json"
    assert cli_models.load_cached_models(cache) == []
    cache.write_text("{ not json")
    assert cli_models.load_cached_models(cache) == []


def test_empty_fetch_does_not_clobber_the_cache(tmp_path):
    cache = tmp_path / "cli_models.json"
    cli_models.save_cached_models([cli_models.ModelInfo("claude-opus-5")], cache)
    cli_models.save_cached_models([], cache)
    assert [m.model_id for m in cli_models.load_cached_models(cache)] == \
        ["claude-opus-5"]


# -- GUI wiring --------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_panel_shows_freshly_discovered_models(qapp):
    panel = AgentPanel()
    panel._apply_models([cli_models.ModelInfo("auto", "Auto"),
                         cli_models.ModelInfo("claude-opus-5", "Claude Opus 5")])
    items = [panel.cli_model_combo.itemText(i)
             for i in range(panel.cli_model_combo.count())]
    assert items == ["auto", "claude-opus-5"]


def test_panel_keeps_the_selected_model_across_a_refresh(qapp):
    panel = AgentPanel()
    panel._apply_models([cli_models.ModelInfo("auto"),
                         cli_models.ModelInfo("claude-opus-5")])
    panel.cli_model_combo.setCurrentText("claude-opus-5")
    panel._apply_models([cli_models.ModelInfo("auto"),
                         cli_models.ModelInfo("claude-opus-5"),
                         cli_models.ModelInfo("gpt-5.6-sol")])
    assert panel.cli_model_combo.currentText() == "claude-opus-5"
    assert panel.current_config().cli_model == "claude-opus-5"


def test_panel_keeps_a_retired_model_id_typed_by_the_user(qapp):
    panel = AgentPanel()
    panel.cli_model_combo.setCurrentText("some-private-model")
    panel._apply_models([cli_models.ModelInfo("auto")])
    # Nothing silently switches the model out from under the user.
    assert panel.cli_model_combo.currentText() == "some-private-model"


def test_panel_ignores_an_empty_refresh(qapp):
    panel = AgentPanel()
    panel._apply_models([cli_models.ModelInfo("auto"),
                         cli_models.ModelInfo("claude-opus-5")])
    panel._on_models_fetched([])
    items = [panel.cli_model_combo.itemText(i)
             for i in range(panel.cli_model_combo.count())]
    assert items == ["auto", "claude-opus-5"]
