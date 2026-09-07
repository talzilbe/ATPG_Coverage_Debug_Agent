"""Run-scoped scratch space for the agent hand-off artefacts.

Every file this tool hands to an external agent -- the serialised evidence,
the MCP server configuration, the CLI working directory, spilled tool payloads
-- lands under one directory that names the design and the run that produced
it, and is removed when the run ends.

Why it is worth a module
------------------------
Flat ``/tmp/atpg_evidence_xxxx.json`` files from previous runs survived
alongside a live run of a completely different, far larger design. An agent
doing filesystem discovery cannot tell a stale 118-fault sample from the
6-million-fault partition it is actually analysing: both are plausible, both
are readable, and nothing in either says which run wrote it. Namespacing plus
a stamp makes that mistake impossible to make silently.

Nothing here is security-sensitive on its own, but the directory is created
with owner-only permissions because it contains a full serialisation of a
design's netlist connectivity.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Environment variable naming an already-created session directory. Set by
#: the parent process so a child (the MCP server) spills into the same place.
SESSION_DIR_ENV = "ATPG_SESSION_DIR"

#: Root under the system temp dir. One level so a stale run is easy to find
#: and easy to delete by hand.
ROOT_NAME = "atpg_debug_sessions"

_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."


def safe_name(value: Optional[str], fallback: str = "design") -> str:
    """Return *value* reduced to characters that are safe in a path segment."""
    text = "".join(c if c in _SAFE else "_" for c in str(value or "")).strip("._")
    return text[:64] or fallback


def new_run_id() -> str:
    """A run identifier that sorts chronologically and cannot collide."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def session_dir(design: Optional[str] = None,
                run_id: Optional[str] = None,
                reuse_env: bool = True) -> str:
    """Create and return this run's scratch directory.

    Args:
        design: Design name, used in the directory name so a stray artefact
            identifies the design it came from.
        run_id: Run identifier; a fresh one is generated when omitted.
        reuse_env: When set and :data:`SESSION_DIR_ENV` names an existing
            directory, return that instead of creating a new one. This is how
            a child process shares the parent's session.

    Returns:
        An absolute path to an existing directory, owner-accessible only.
    """
    if reuse_env:
        existing = os.environ.get(SESSION_DIR_ENV, "")
        if existing and os.path.isdir(existing):
            return existing
    root = os.path.join(tempfile.gettempdir(), ROOT_NAME)
    path = os.path.join(root, f"{safe_name(design)}_{run_id or new_run_id()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def stamp(design: Optional[str] = None,
          sources: Optional[Dict[str, Any]] = None,
          run_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the provenance stamp attached to every artefact.

    An artefact that does not say which design and which input files it came
    from is indistinguishable from a stale one lying next to it.
    """
    src = dict(sources or {})
    return {
        "design": design or src.get("design") or "unknown",
        "run_id": run_id or new_run_id(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "netlist": src.get("netlist"),
        "faults": src.get("faults"),
        "constraints": src.get("constraints"),
        "note": ("This artefact belongs to the run named above. Any file "
                 "outside this directory, or carrying a different design or "
                 "run_id, is from another run and must not be read as input "
                 "to this one."),
    }


def cleanup(path: Optional[str]) -> None:
    """Remove a session directory, ignoring anything that goes wrong.

    Cleanup failure must never break an analysis run, but it is logged: a
    directory that keeps failing to disappear is exactly the stale artefact
    this module exists to prevent.
    """
    if not path:
        return
    root = os.path.join(tempfile.gettempdir(), ROOT_NAME)
    if not os.path.abspath(path).startswith(os.path.abspath(root)):
        logger.warning("Refusing to remove '%s': not inside %s", path, root)
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("Could not remove session directory '%s': %s",
                       path, exc)
