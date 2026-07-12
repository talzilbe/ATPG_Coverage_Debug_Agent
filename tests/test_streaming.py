"""Tests for streaming output (HTTP SSE parsing and CLI Popen streaming)."""

from __future__ import annotations

import os
import sys

from atpg_coverage_debug_agent.agent import debug_agent
from atpg_coverage_debug_agent.agent.debug_agent import AgentConfig, DebugAgent


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


def test_post_stream_parses_sse(monkeypatch):
    lines = [
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
        b'data: {"choices":[{"delta":{}}]}\n',
        b'data: [DONE]\n',
    ]
    monkeypatch.setattr(debug_agent.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(lines))
    agent = DebugAgent(AgentConfig(backend="http", base_url="http://x/v1",
                                   model="m"))
    chunks = []
    out = agent._post_stream([{"role": "user", "content": "hi"}],
                             lambda c: chunks.append(c))
    assert out == "Hello"
    assert chunks == ["Hel", "lo"]


def test_call_cli_streaming_emits_and_returns(tmp_path):
    scratch = os.path.join(tmp_path, "s")
    os.makedirs(scratch, exist_ok=True)
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=sys.executable,
                                   timeout=30))
    script = ("import sys\n"
              "for w in ['Hello', ' ', 'world']:\n"
              "    sys.stdout.write(w); sys.stdout.flush()\n")
    cmd = [sys.executable, "-c", script]
    chunks = []
    out = agent._call_cli_streaming(cmd, dict(os.environ), scratch,
                                    lambda c: chunks.append(c))
    assert out == "Hello world"
    assert "".join(chunks) == "Hello world"


def test_call_cli_streaming_nonzero_exit_raises(tmp_path):
    scratch = os.path.join(tmp_path, "s2")
    os.makedirs(scratch, exist_ok=True)
    agent = DebugAgent(AgentConfig(backend="cli", cli_path=sys.executable,
                                   timeout=30))
    cmd = [sys.executable, "-c",
           "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    try:
        agent._call_cli_streaming(cmd, dict(os.environ), scratch, lambda c: None)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "exited 3" in str(exc)
