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

import copy
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import session
from .analysis import investigate

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "atpg-coverage-debug"
SERVER_VERSION = "1.0.0"

_JSON_TYPES = {"int": "integer", "float": "number", "bool": "boolean",
               "str": "string"}

#: Largest tool response, in characters, that is returned inline. Beyond it
#: the response is shrunk and the complete payload is spilled to a file. The
#: default is deliberately generous; override with
#: ``ATPG_MCP_MAX_INLINE_CHARS`` to match a client's own limit.
DEFAULT_MAX_INLINE_CHARS = 60000

#: Keys carrying prose, samples or per-fault detail. These are dropped FIRST,
#: in this order, because a complete set of counts with no samples is worth
#: far more to a reader than three verbose entries out of twenty-five. The
#: reverse choice is what produced a 500-character preview of a census.
VERBOSE_KEYS: Tuple[str, ...] = (
    "evidence", "observed_facts", "inferred_conclusions", "mapping_evidence",
    "mapping_candidates", "scan_evidence", "instantiation", "raw_text",
    "samples", "sample_faults", "fan_in", "fan_out", "commands", "caveats",
    "rationale", "expected_effect", "note", "notes", "description",
    "instruction", "acknowledged", "retrieval_hint",
)

#: Keys that must survive every level of shrinking. Counts and the checks
#: computed over them are the whole point of the payload.
PROTECTED_KEYS: Tuple[str, ...] = (
    "count", "counts", "total", "totals", "census", "census_check",
    "reconciliation", "classes", "categories", "roles", "coverage_metrics",
    "metrics", "subclass", "subclasses", "family", "role", "pct", "sa0",
    "sa1", "delta", "reconciles", "total_faults", "error", "verdict",
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state(evidence_path: Optional[str] = None) -> Dict[str, Any]:
    """Load and rehydrate the evidence file into a server state dict."""
    path = evidence_path or os.environ.get("ATPG_EVIDENCE_FILE", "")
    faults: List[Any] = []
    constraints: List[Any] = []
    adjacency: Dict[str, List[str]] = {}
    triage: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    design: Optional[Dict[str, Any]] = None
    stamp: Optional[Dict[str, Any]] = None
    load_error = ""
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                evidence = json.load(fh)
            faults, constraints, adjacency = investigate.rehydrate(evidence)
            compare = evidence.get("compare")
            triage = evidence.get("triage")
            context = evidence.get("context")
            design = evidence.get("design")
            stamp = evidence.get("stamp")
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
        "triage": triage,
        "context": context,
        "design": design,
        "stamp": stamp,
        "evidence_path": path,
        "load_error": load_error,
        "initialized": False,
    }


def max_inline_chars() -> int:
    """The inline response budget, overridable from the environment."""
    raw = os.environ.get("ATPG_MCP_MAX_INLINE_CHARS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INLINE_CHARS
    return value if value > 0 else DEFAULT_MAX_INLINE_CHARS


# ---------------------------------------------------------------------------
# Self-describing truncation
# ---------------------------------------------------------------------------
def _encode(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _drop_verbose(node: Any, key: str, removed: List[str],
                  path: str = "") -> Any:
    """Remove every occurrence of *key* from *node*, recording where."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if k == key and k not in PROTECTED_KEYS:
                removed.append(here)
                continue
            out[k] = _drop_verbose(v, key, removed, here)
        return out
    if isinstance(node, list):
        return [_drop_verbose(v, key, removed, f"{path}[]") for v in node]
    return node


def _longest_list(node: Any, path: str = "") -> Tuple[str, int]:
    """Locate the longest unprotected list in *node*, for shortening."""
    best = ("", 0)
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if isinstance(v, list) and k not in PROTECTED_KEYS:
                if len(v) > best[1]:
                    best = (here, len(v))
            deeper = _longest_list(v, here)
            if deeper[1] > best[1]:
                best = deeper
    elif isinstance(node, list):
        for i, v in enumerate(node):
            deeper = _longest_list(v, f"{path}[{i}]")
            if deeper[1] > best[1]:
                best = deeper
    return best


def _shorten_list_at(node: Any, target: str, keep: int, path: str = "") -> bool:
    """Truncate the list at dotted *target* to *keep* entries, in place."""
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if here == target and isinstance(v, list):
                del v[keep:]
                return True
            if _shorten_list_at(v, target, keep, here):
                return True
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if _shorten_list_at(v, target, keep, f"{path}[{i}]"):
                return True
    return False


def shrink_payload(data: Dict[str, Any], limit: int,
                   spill_dir: Optional[str] = None,
                   tool_name: str = "tool") -> Dict[str, Any]:
    """Return *data* small enough to send inline, saying what it dropped.

    A truncation that does not describe itself is worse than no answer: the
    reader cannot tell an abridged census from a complete one, and closes the
    arithmetic on a subset. So the complete payload is written to a file, the
    inline copy is shrunk by dropping the *least* informative content first,
    and the result carries a ``_truncation`` block naming what was removed and
    where the whole thing lives.

    Shrinking order, cheapest information first:

    1. prose and per-fault detail (:data:`VERBOSE_KEYS`);
    2. the longest remaining list, repeatedly;
    3. never counts. Keys in :data:`PROTECTED_KEYS` are exempt at every
       stage, so a census keeps all of its rows while its samples go.

    Args:
        data: The tool result.
        limit: Inline character budget.
        spill_dir: Where to write the complete payload. When omitted, no
            spill file is written and the response says so.
        tool_name: Used in the spill file name.

    Returns:
        The inline payload, unchanged when it already fits.
    """
    text = _encode(data)
    if len(text) <= limit:
        return data

    spill_path = ""
    if spill_dir:
        try:
            os.makedirs(spill_dir, mode=0o700, exist_ok=True)
            spill_path = os.path.join(
                spill_dir, f"{session.safe_name(tool_name, 'tool')}"
                           f"_{os.getpid()}_{abs(hash(text)) % 10**8}.json")
            with open(spill_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            spill_path = ""

    shrunk = copy.deepcopy(data)
    omitted: List[str] = []
    for key in VERBOSE_KEYS:
        if len(_encode(shrunk)) <= limit:
            break
        removed: List[str] = []
        shrunk = _drop_verbose(shrunk, key, removed)
        if removed:
            omitted.append(f"{key} (x{len(removed)})")

    # Still too big: shorten the longest non-count list, repeatedly. Halving
    # rather than clipping to a fixed size keeps the shape of the data
    # recognisable for as long as possible.
    guard = 0
    while len(_encode(shrunk)) > limit and guard < 40:
        guard += 1
        target, length = _longest_list(shrunk)
        if not target or length <= 1:
            break
        keep = max(1, length // 2)
        if _shorten_list_at(shrunk, target, keep):
            omitted.append(f"{target}[{keep}:] ({length - keep} entries)")

    shrunk["_truncation"] = {
        "truncated": True,
        "reason": (f"The complete response is {len(text)} characters, over "
                   f"the {limit}-character inline limit."),
        "omitted_fields": omitted or ["(nothing could be dropped safely)"],
        "counts_preserved": True,
        "spill_path": spill_path or None,
        "retrieval_hint": (
            f"Read {spill_path} for the complete payload before drawing any "
            f"conclusion from this response." if spill_path else
            "The complete payload could not be written to disk. Re-query "
            "with a narrower filter or a smaller limit."),
        "warning": (
            "THIS IS A TRUNCATED RESULT, NOT A READ RESULT. Counts, "
            "categories and the census were preserved; samples, prose and "
            "per-fault detail were dropped first. Do not treat any list here "
            "as exhaustive unless it is a count list, and do not report a "
            "question as unresolved on the strength of a truncated "
            "response -- retrieve the spill first."),
    }
    return shrunk


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
                triage=state.get("triage"),
                context=state.get("context"),
                design=state.get("design"),
            )
        except Exception as exc:  # noqa: BLE001
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "isError": True,
            })
        data = shrink_payload(
            data, max_inline_chars(),
            spill_dir=os.path.join(session.session_dir(
                (state.get("stamp") or {}).get("design")), "spill"),
            tool_name=name)
        text = _encode(data)
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
