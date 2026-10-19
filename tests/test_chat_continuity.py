"""A carried-over chat continues with its history as context; context meter; /compact."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from atpg_coverage_debug_agent.agent import cth_models  # noqa: E402
from atpg_coverage_debug_agent.agent import debug_agent as da  # noqa: E402
from atpg_coverage_debug_agent.app import run_analysis  # noqa: E402
from atpg_coverage_debug_agent.gui import agent_panel as ap  # noqa: E402

_SAVED = {"diagnosis": "AU.PC is driven by pi_hold.",
          "chat": [{"role": "You", "text": "why is io_blk lost?"},
                   {"role": "Agent", "text": "Its input is held at C1."},
                   {"role": "Error", "text": "timeout"}],
          "trace": "", "findings": [], "fix_plan_edits": []}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp, sample_netlist_path, sample_faults_path,
          sample_constraints_path, monkeypatch):
    monkeypatch.setattr(ap.AgentPanel, "refresh_models", lambda self: None)
    monkeypatch.setattr(QThread, "start", lambda self, *a: None)
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    p = ap.AgentPanel()
    p.set_report(rep, None)
    p.cli_path_edit.setText(__file__)
    p.mode_combo.setCurrentIndex(p.mode_combo.findData("quick"))
    yield p
    p._chat_thread = None
    p._chat_worker = None
    p.shutdown()


def _send(p, text):
    p.chat_input.setText(text)
    p.on_send_chat()
    return p._chat_worker


# -- the seed --------------------------------------------------------------------

def test_seed_carries_report_diagnosis_and_turns_but_not_errors(panel):
    turns = [(t["role"], t["text"]) for t in _SAVED["chat"]]
    seed = da.conversation_seed(panel._report, 50, _SAVED["diagnosis"], turns)
    assert seed.startswith(da.build_user_payload(panel._report, 50))
    assert da.EARLIER_CONVERSATION_HEADER in seed
    assert "pi_hold" in seed and "why is io_blk lost?" in seed
    assert "held at C1" in seed and "timeout" not in seed
    compact = da.conversation_seed(panel._report, 50, "OLD DIAGNOSIS", [],
                                   summary="- the summary")
    assert "the summary" in compact and "OLD DIAGNOSIS" not in compact


def test_seeded_cli_turn_starts_a_new_session_from_the_seed(monkeypatch):
    calls = []
    agent = da.DebugAgent(da.AgentConfig(backend="cli", cli_path=__file__))
    monkeypatch.setattr(agent, "_call_cli",
                        lambda *a, **k: calls.append((a, k)) or "ok")
    assert agent.chat("next?", session_id="new", seed="SEED") == "ok"
    (system, payload), kw = calls[0]
    assert system == da.SYSTEM_PROMPT
    assert payload.startswith("SEED\n\n" + da.FOLLOW_UP_INSTRUCTION)
    assert payload.endswith("next?")
    assert kw["session_id"] == "new" and not kw.get("resume")


def test_compact_turn_is_sent_as_is_without_a_guardrail_pass(monkeypatch):
    calls = []
    agent = da.DebugAgent(da.AgentConfig(backend="cli", cli_path=__file__))
    monkeypatch.setattr(agent, "_call_cli",
                        lambda *a, **k: calls.append(a[1]) or "- s")
    monkeypatch.setattr(agent, "correct_guardrail_issues",
                        lambda *a: pytest.fail("no guardrail on a summary"))
    agent.chat(da.COMPACT_INSTRUCTION, session_id="s", report=object(),
               wrap=False, check=False)
    assert calls == [da.COMPACT_INSTRUCTION]


# -- the panel -------------------------------------------------------------------

def test_a_restored_chat_can_be_continued_with_its_history(panel):
    panel.import_investigation(_SAVED)
    assert panel.chat_input.isEnabled()
    assert "continues in a new session" in panel.status_label.text()
    assert panel._session_id is None and "held at C1" in panel._chat_seed
    worker = _send(panel, "and the fix?")
    assert worker._seed == panel._chat_seed
    assert worker._session_id == panel._pending_session
    panel._on_chat_finished("Release pi_hold.")
    assert panel._session_id == worker._session_id
    assert panel._chat_seed is None and panel._pending_session is None
    panel._chat_thread = panel._chat_worker = None
    # The next question resumes that session normally.
    worker = _send(panel, "anything else?")
    assert worker._seed is None and worker._session_id == panel._session_id


def test_a_failed_first_turn_keeps_the_seed_for_a_retry(panel):
    panel.import_investigation(_SAVED)
    seed = panel._chat_seed
    _send(panel, "and the fix?")
    panel._on_chat_failed("network down")
    assert panel._chat_seed == seed and panel._pending_session is None


def test_http_restore_replays_the_conversation_as_messages(panel):
    panel.backend_combo.setCurrentIndex(panel.backend_combo.findData("http"))
    panel.import_investigation(_SAVED)
    roles = [m["role"] for m in panel._chat_messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert panel._chat_messages[2]["content"] == _SAVED["diagnosis"]


def test_meter_shows_an_estimate_and_warns_when_full(panel, monkeypatch):
    assert panel.context_row_widget.isHidden()
    panel.import_investigation(_SAVED)
    assert not panel.context_row_widget.isHidden()
    assert "window assumed" in panel.context_bar.format()
    used = panel._ctx_chars // ap._CHARS_PER_TOKEN
    assert ap._tokens_k(used) in panel.context_bar.format()
    panel._ctx_reset(int(ap._DEFAULT_CONTEXT_TOKENS * ap._CHARS_PER_TOKEN
                         * 0.9))
    assert panel.context_bar.value() == 90
    assert "#c62828" in panel.context_bar.styleSheet()
    assert "/compact" in panel.context_bar.toolTip()
    free = cth_models.FreeModel("g", "G", context_length=1_000_000)
    monkeypatch.setattr(panel, "selected_free_model", lambda: free)
    panel._update_context_meter()
    assert "window assumed" not in panel.context_bar.format()
    assert "1,000k" in panel.context_bar.format()


def test_tool_results_count_toward_the_meter(panel, tmp_path):
    log = tmp_path / "tool_calls.jsonl"
    log.write_text('{"tool": "list_faults", "args": {}, "chars": 4000}\n')

    class Sess:
        tool_log_path = str(log)
    panel._mcp_session = Sess()
    before = panel._ctx_chars
    panel._poll_tool_log()
    assert panel._ctx_chars - before >= 4000
    panel._mcp_session = None


def test_slash_compact_summarises_and_continues_from_the_summary(panel):
    panel.import_investigation(_SAVED)
    big = panel._ctx_chars
    worker = _send(panel, "/compact")
    assert worker._message == da.COMPACT_INSTRUCTION
    assert not worker._wrap and not worker._check
    assert panel._chat_turns[-1] == ("You", "/compact")
    panel._on_chat_finished("- pi_hold holds io_blk at C1")
    assert panel._chat_turns[-1] == ("Summary", "- pi_hold holds io_blk at C1")
    assert "pi_hold holds io_blk" in panel._chat_seed
    assert "why is io_blk lost?" not in panel._chat_seed
    assert panel._session_id is None
    assert panel._ctx_chars < big + len(da.COMPACT_INSTRUCTION)
    assert "compacted" in panel.status_label.text()
    assert panel.chat_input.isEnabled()


def test_restoring_after_a_compaction_starts_from_the_last_summary(panel):
    saved = dict(_SAVED, chat=_SAVED["chat"] + [
        {"role": "You", "text": "/compact"},
        {"role": "Summary", "text": "- SUMMARY"},
        {"role": "You", "text": "later question"}])
    panel.import_investigation(saved)
    assert "SUMMARY" in panel._chat_seed
    assert "later question" in panel._chat_seed
    assert "why is io_blk lost?" not in panel._chat_seed


def test_compact_without_a_conversation_says_so(panel):
    panel.compact_chat()
    assert "Nothing to compact" in panel.status_label.text()
    assert panel._chat_worker is None
