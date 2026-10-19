"""Carry the open work over when the GUI reopens itself inside Cth.ai."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from ..models import AnalysisReport
from ..reporting.session_report import load_report, save_report

MANIFEST = "manifest.json"
STARTED = "started"
READY = "ready"
FAILED = "failed"

#: How long the old window waits for the Cth.ai setup to open the new one.
TIMEOUT_S = 180


def write_snapshot(parent: Path, partitions: List[dict], active_idx: int,
                   report: Optional[AnalysisReport],
                   base_report: Optional[AnalysisReport],
                   tab: str, visualizer: Optional[dict] = None) -> Path:
    """Save the open reports into a new private directory; return the manifest.

    *partitions* holds ``{"name", "report", "base_report"}`` dicts (a
    multi-partition run); otherwise the single *report* is saved.
    *visualizer* says where the running Tessent session can be reached.
    """
    folder = Path(tempfile.mkdtemp(prefix="reopen_", dir=str(parent)))
    entries = []
    items = partitions or ([{"name": "", "report": report,
                             "base_report": base_report}] if report else [])
    for i, part in enumerate(items):
        entry = {"name": part.get("name", "")}
        save_report(part["report"], str(folder / f"report_{i}.json"))
        entry["report"] = f"report_{i}.json"
        base = part.get("base_report")
        if base is not None and base is not part["report"]:
            save_report(base, str(folder / f"base_{i}.json"))
            entry["base_report"] = f"base_{i}.json"
        entries.append(entry)
    manifest = {"version": 1, "created": time.time(),
                "multi": bool(partitions), "active": max(active_idx, 0),
                "tab": tab, "reports": entries, "visualizer": visualizer}
    path = folder / MANIFEST
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return path


def read_snapshot(manifest: str) -> dict:
    """Load a snapshot: ``{"multi", "active", "tab", "partitions",
    "visualizer"}``.

    Each partition is ``{"name", "report", "base_report"}``.
    """
    folder = Path(manifest).parent
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    parts = []
    for entry in data.get("reports") or []:
        rep = load_report(str(folder / entry["report"]))
        base = (load_report(str(folder / entry["base_report"]))
                if entry.get("base_report") else rep)
        parts.append({"name": entry.get("name", ""), "report": rep,
                      "base_report": base})
    return {"multi": bool(data.get("multi")),
            "active": int(data.get("active") or 0),
            "tab": str(data.get("tab") or ""), "partitions": parts,
            "visualizer": data.get("visualizer") or None}


def note(message: str) -> None:
    """A progress line for the hand-off log (the new window's stderr)."""
    print(f"[reopen {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr,
          flush=True)


def mark_started(manifest: str) -> None:
    (Path(manifest).parent / STARTED).write_text(str(os.getpid()),
                                                 encoding="utf-8")


def is_started(manifest: Path) -> bool:
    return (manifest.parent / STARTED).is_file()


def mark_ready(manifest: str) -> None:
    (Path(manifest).parent / READY).write_text(str(os.getpid()),
                                               encoding="utf-8")


def is_ready(manifest: Path) -> bool:
    return (manifest.parent / READY).is_file()


def mark_failed(manifest: str, message: str) -> None:
    (Path(manifest).parent / FAILED).write_text(message, encoding="utf-8")


def failure(manifest: Path) -> Optional[str]:
    """The new window's error message, if it could not load the snapshot."""
    try:
        return (manifest.parent / FAILED).read_text(encoding="utf-8")
    except OSError:
        return None


def log_tail(path: Path, lines: int = 15) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def discard(manifest: Path) -> None:
    shutil.rmtree(manifest.parent, ignore_errors=True)
