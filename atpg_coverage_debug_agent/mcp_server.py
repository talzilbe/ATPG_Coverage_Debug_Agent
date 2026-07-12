"""Minimal Model Context Protocol (MCP) stdio server exposing the ATPG
investigative tools.

The GitHub Copilot CLI launches this module as a subprocess (configured via
``--additional-mcp-config``) and talks JSON-RPC 2.0 over newline-delimited
stdio. The server loads a serialised *evidence* file (path in the
``ATPG_EVIDENCE_FILE`` environment variable), rehydrates it, and answers
``tools/call`` requests using the exact same deterministic query core
(:mod:`atpg_coverage_debug_agent.analysis.investigate`) that backs the HTTP
tool-calling agent — so both backends behave identically.

This module has **no third-party dependencies**: the small slice of the MCP
protocol needed (initialize / tools/list / tools/call / ping) is implemented
with the standard library so it is easy to run and unit-test.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .analysis import investigate

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "atpg-coverage-debug"
SERVER_VERSION = "1.0.0"

_JSON_TYPES = {"int": "integer", "float": "number", "bool": "boolean",
               "str": "string"}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state(evidence_path: Optional[str] = None) -> Dict[str, Any]:
    """Load and rehydrate the evidence file into a server state dict."""
    path = evidence_path or os.environ.get("ATPG_EVIDENCE_FILE", "")
    faults: List[Any] = []
    constraints: List[Any] = []
    adjacency: Dict[str, List[str]] = {}
    load_error = ""
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                evidence = json.load(fh)
            faults, constraints, adjacency = investigate.rehydrate(evidence)
            compare = evidence.get("compare")
        except Exception as exc:  # noqa: BLE001
            load_error = f"Failed to load evidence file '{path}': {exc}"
            compare = None
    else:
        load_error = f"Evidence file not found: {path!r}"
        compare = None
    return {
        "faults": faults,
        "constraints": constraints,
        "adjacency": adjacency,
        "compare": compare,
        "load_error": load_error,
        "initialized": False,
    }


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------
def build_tools_list() -> List[Dict[str, Any]]:
    """Build MCP tool definitions from the shared TOOL_SPECS."""
    tools: List[Dict[str, Any]] = []
    for name, spec in investigate.TOOL_SPECS.items():
        properties: Dict[str, Any] = {}
        for pname, pspec in spec.get("params", {}).items():
            prop = {
                "type": _JSON_TYPES.get(pspec.get("type", "str"), "string"),
                "description": pspec.get("description", ""),
            }
            if "default" in pspec:
                prop["description"] += f" (default: {pspec['default']})"
            properties[pname] = prop
        tools.append({
            "name": name,
            "description": spec.get("description", name),
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": [],
            },
        })
    return tools


# ---------------------------------------------------------------------------
# JSON-RPC handling
# ---------------------------------------------------------------------------
def _result(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code,
                                                       "message": message}}


def handle_message(msg: Dict[str, Any],
                   state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message. Returns a response dict, or None for
    notifications (messages without an ``id``)."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        state["initialized"] = True
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(msg_id, {})

    if method == "tools/list":
        return _result(msg_id, {"tools": build_tools_list()})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in investigate.TOOL_SPECS:
            return _error(msg_id, -32602, f"Unknown tool '{name}'.")
        try:
            data = investigate.run_tool(
                name, arguments,
                fault_results=state["faults"],
                constraints=state["constraints"],
                netlist=None,
                adjacency=state["adjacency"],
                compare=state.get("compare"),
            )
        except Exception as exc:  # noqa: BLE001
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "isError": True,
            })
        text = json.dumps(data, indent=2, default=str)
        return _result(msg_id, {"content": [{"type": "text", "text": text}]})

    if is_notification:
        return None
    return _error(msg_id, -32601, f"Method not found: {method}")


def serve(stdin=None, stdout=None, state: Optional[Dict[str, Any]] = None) -> int:
    """Run the newline-delimited JSON-RPC stdio loop until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if state is None:
        state = load_state()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle_message(msg, state)
        except Exception as exc:  # noqa: BLE001
            response = _error(msg.get("id"), -32603, f"Internal error: {exc}")
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
