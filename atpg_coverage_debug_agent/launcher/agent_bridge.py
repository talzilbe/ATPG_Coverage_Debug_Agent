"""Hand-off between the agent's tool server and the GUI for Tessent commands.

The MCP server runs as a child of the Copilot CLI and never holds the control
channel's token. When the agent asks to run something in the live Tessent
session, the server writes a *request* file into the conversation's session
directory and waits. The GUI picks it up, shows it to the user, and -- only if
approved -- sends it over the channel and writes a *response* file. So the
approval is structural: no request reaches the tool without the GUI.

Files, all in ``<session dir>/tessent_bridge`` (0700):

* ``req_<id>.json``       written by the server: kind, script, reason
* ``resp_<id>.json``      written by the GUI: the outcome
* ``withdrawn_<id>``      written by the server when it stopped waiting

Every write is atomic (temp file + rename), so a reader never sees half a file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

BRIDGE_DIR = "tessent_bridge"

#: Seconds the server waits for the user to approve a script.
DEFAULT_APPROVAL_TIMEOUT = 600.0

#: Seconds the server waits for the GUI to answer a status request.
STATUS_TIMEOUT = 15.0

APPROVAL_TIMEOUT_ENV = "ATPG_TESSENT_APPROVAL_TIMEOUT"

#: Largest transcript returned inline; the complete text is written beside it.
MAX_INLINE_TRANSCRIPT = 40000

#: Seconds between progress callbacks while waiting.
PROGRESS_INTERVAL = 5.0

KIND_STATUS = "status"
KIND_RUN = "run"

_ID_RE = re.compile(r"^[a-f0-9]{12}$")

#: Tools exposed only by the MCP server (the Copilot CLI backend): they need
#: the GUI on the other end of the bridge.
TESSENT_TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "tessent_session_status": {
        "description": (
            "Whether a live Tessent Visualizer session is attached to this "
            "conversation, whether it accepts agent commands, and which design "
            "inputs it was launched with. Call this before tessent_run. Needs "
            "no approval."),
        "params": {},
    },
    "tessent_run": {
        "description": (
            "Run a Tcl/Tessent script in the user's live Tessent session and "
            "return its result plus the transcript it printed. EVERY call is "
            "shown to the user, who approves, edits or rejects it, so give a "
            "one-line reason. Use it for evidence the offline analysis cannot "
            "give (report_*, get_*, analyze_fault, add_schematic_* to show "
            "something). The response says whether the script was edited "
            "before it ran ('script_ran' is what actually executed)."),
        "params": {
            "script": {"type": "str",
                       "description": "Tcl to run at the tool prompt."},
            "reason": {"type": "str",
                       "description": "Why you need it, shown to the user."},
        },
    },
}


class BridgeError(Exception):
    """The bridge directory is missing or a request is malformed."""


@dataclass
class TessentRequest:
    id: str
    kind: str
    script: str = ""
    reason: str = ""
    created: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "script": self.script,
                "reason": self.reason, "created": self.created}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TessentRequest":
        return cls(id=str(data.get("id", "")), kind=str(data.get("kind", "")),
                   script=str(data.get("script", "")),
                   reason=str(data.get("reason", "")),
                   created=float(data.get("created", 0.0) or 0.0))


def bridge_dir(work_dir: str, create: bool = True) -> str:
    path = os.path.join(work_dir, BRIDGE_DIR)
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=str)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _check_id(req_id: str) -> str:
    if not _ID_RE.match(req_id or ""):
        raise BridgeError(f"not a valid request id: {req_id!r}")
    return req_id


def _req_path(bridge: str, req_id: str) -> str:
    return os.path.join(bridge, f"req_{_check_id(req_id)}.json")


def _resp_path(bridge: str, req_id: str) -> str:
    return os.path.join(bridge, f"resp_{_check_id(req_id)}.json")


def _withdrawn_path(bridge: str, req_id: str) -> str:
    return os.path.join(bridge, f"withdrawn_{_check_id(req_id)}")


def submit(bridge: str, kind: str, script: str = "",
           reason: str = "") -> TessentRequest:
    """Write a request for the GUI and return it."""
    if kind not in (KIND_STATUS, KIND_RUN):
        raise BridgeError(f"unknown request kind: {kind!r}")
    if kind == KIND_RUN and not (script or "").strip():
        raise BridgeError("no script given")
    req = TessentRequest(id=secrets.token_hex(6), kind=kind,
                         script=(script or "").strip(),
                         reason=(reason or "").strip(), created=time.time())
    _write_json(_req_path(bridge, req.id), req.as_dict())
    return req


def respond(bridge: str, req_id: str, payload: Dict[str, Any]) -> None:
    """Write the GUI's answer to *req_id*."""
    data = dict(payload)
    data["id"] = req_id
    _write_json(_resp_path(bridge, req_id), data)


def response(bridge: str, req_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(_resp_path(bridge, req_id))


def withdraw(bridge: str, req_id: str) -> None:
    try:
        with open(_withdrawn_path(bridge, req_id), "w", encoding="utf-8"):
            pass
    except OSError:
        pass


def is_withdrawn(bridge: str, req_id: str) -> bool:
    return os.path.exists(_withdrawn_path(bridge, req_id))


def pending(bridge: str) -> List[TessentRequest]:
    """Requests with no answer that the server is still waiting on, oldest first."""
    try:
        names = os.listdir(bridge)
    except OSError:
        return []
    out: List[TessentRequest] = []
    for name in names:
        if not (name.startswith("req_") and name.endswith(".json")):
            continue
        req_id = name[4:-5]
        if not _ID_RE.match(req_id):
            continue
        if (os.path.exists(_resp_path(bridge, req_id))
                or is_withdrawn(bridge, req_id)):
            continue
        data = _read_json(os.path.join(bridge, name))
        if data is None:
            continue
        out.append(TessentRequest.from_dict(data))
    out.sort(key=lambda r: r.created)
    return out


def approval_timeout() -> float:
    raw = os.environ.get(APPROVAL_TIMEOUT_ENV, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_TIMEOUT
    return value if value > 0 else DEFAULT_APPROVAL_TIMEOUT


def wait_for_response(bridge: str, req_id: str, timeout: float,
                      poll: float = 0.25,
                      on_tick: Optional[Callable[[float], None]] = None,
                      ) -> Dict[str, Any]:
    """Block until the GUI answers *req_id*, or withdraw it after *timeout*."""
    started = time.monotonic()
    last_tick = started
    while True:
        data = response(bridge, req_id)
        if data is not None:
            return data
        now = time.monotonic()
        if now - started >= timeout:
            withdraw(bridge, req_id)
            data = response(bridge, req_id)
            if data is not None:
                return data
            return {"id": req_id, "status": "timeout",
                    "message": (f"No answer from the GUI within {timeout:.0f} s. "
                                "The request was withdrawn and did NOT run.")}
        if on_tick is not None and now - last_tick >= PROGRESS_INTERVAL:
            last_tick = now
            try:
                on_tick(now - started)
            except Exception:  # noqa: BLE001 - progress is best-effort
                pass
        time.sleep(poll)


def call_tool(name: str, arguments: Dict[str, Any], work_dir: str,
              on_tick: Optional[Callable[[float], None]] = None,
              ) -> Dict[str, Any]:
    """Serve one ``tessent_*`` tool call from the MCP server side."""
    if not work_dir or not os.path.isdir(work_dir):
        return {"status": "unavailable",
                "message": "This tool server has no session directory, so it "
                           "cannot reach the GUI. Tessent commands are not "
                           "available in this conversation."}
    bridge = bridge_dir(work_dir)
    if name == "tessent_session_status":
        req = submit(bridge, KIND_STATUS)
        data = wait_for_response(bridge, req.id, STATUS_TIMEOUT, on_tick=on_tick)
        if data.get("status") == "timeout":
            data["message"] = ("The GUI did not answer. Tessent commands work "
                               "only while the GUI that started this "
                               "conversation is open.")
        return data
    if name == "tessent_run":
        script = str(arguments.get("script", "") or "")
        if not script.strip():
            return {"status": "error", "message": "'script' is required."}
        req = submit(bridge, KIND_RUN, script, str(arguments.get("reason", "")))
        return wait_for_response(bridge, req.id, approval_timeout(),
                                 on_tick=on_tick)
    raise BridgeError(f"unknown tessent tool: {name}")


def cap_transcript(text: str, spill_path: str = "") -> Dict[str, Any]:
    """Keep the head and tail of a long transcript; write all of it to a file."""
    if len(text) <= MAX_INLINE_TRANSCRIPT:
        return {"transcript": text, "transcript_truncated": False}
    full_path = ""
    if spill_path:
        try:
            fd = os.open(spill_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            full_path = spill_path
        except OSError:
            full_path = ""
    half = MAX_INLINE_TRANSCRIPT // 2
    omitted = len(text) - 2 * half
    return {
        "transcript": (text[:half] + f"\n... [{omitted} characters omitted] ...\n"
                       + text[-half:]),
        "transcript_truncated": True,
        "transcript_full_path": full_path or None,
    }
