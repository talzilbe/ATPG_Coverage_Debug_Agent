"""Tests for the MCP stdio server exposing the investigative tools."""

from __future__ import annotations

import io
import json

from atpg_coverage_debug_agent import mcp_server
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.app import run_analysis


def _state(nl, fl, cn):
    rep = run_analysis(nl, fl, cn)
    evidence = investigate.export_evidence(
        rep.fault_results, rep.constraints, rep.netlist)
    faults, constraints, adjacency = investigate.rehydrate(evidence)
    return {
        "faults": faults,
        "constraints": constraints,
        "adjacency": adjacency,
        "load_error": "",
        "initialized": False,
    }, rep


def test_initialize_and_tools_list(sample_netlist_path, sample_faults_path,
                                   sample_constraints_path):
    state, _ = _state(sample_netlist_path, sample_faults_path,
                      sample_constraints_path)
    init = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, state)
    assert init["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert init["result"]["serverInfo"]["name"] == mcp_server.SERVER_NAME

    listed = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, state)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"list_faults", "get_fault_detail", "why_blocked",
            "list_constraints", "trace_path"} <= names
    for tool in listed["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_notification_returns_none(sample_netlist_path, sample_faults_path,
                                   sample_constraints_path):
    state, _ = _state(sample_netlist_path, sample_faults_path,
                      sample_constraints_path)
    resp = mcp_server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, state)
    assert resp is None


def test_tools_call_list_faults(sample_netlist_path, sample_faults_path,
                                sample_constraints_path):
    state, rep = _state(sample_netlist_path, sample_faults_path,
                        sample_constraints_path)
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "list_faults", "arguments": {"limit": 2}},
    }, state)
    content = resp["result"]["content"][0]["text"]
    data = json.loads(content)
    assert data["total_matched"] == len(rep.fault_results)
    assert data["returned"] <= 2


def test_tools_call_unknown_tool(sample_netlist_path, sample_faults_path,
                                 sample_constraints_path):
    state, _ = _state(sample_netlist_path, sample_faults_path,
                      sample_constraints_path)
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "does_not_exist", "arguments": {}},
    }, state)
    assert "error" in resp


def test_serve_loop_over_stdio(sample_netlist_path, sample_faults_path,
                               sample_constraints_path):
    state, _ = _state(sample_netlist_path, sample_faults_path,
                      sample_constraints_path)
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    stdin = io.StringIO(requests)
    stdout = io.StringIO()
    mcp_server.serve(stdin=stdin, stdout=stdout, state=state)
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    # initialize + tools/list produce responses; the notification does not.
    assert len(lines) == 2
    ids = {json.loads(ln)["id"] for ln in lines}
    assert ids == {1, 2}
