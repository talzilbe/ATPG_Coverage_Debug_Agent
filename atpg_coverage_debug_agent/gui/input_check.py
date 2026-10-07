"""Cheap, read-only checks of the Analyze inputs, shown beside each file box.

Only the first few kilobytes of a file are read, so a check stays instant even
for a multi-gigabyte netlist on NFS.
"""

from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass

from ..parser.fault_parser import _is_mtfi, detect_compression, open_fault_list

#: Lines sniffed from the head of a file.
SNIFF_LINES = 200

OK, WARN, ERROR, EMPTY = "ok", "warn", "error", "empty"

_MODULE_RE = re.compile(r"^\s*module\s+\w+", re.MULTILINE)


@dataclass
class InputCheck:
    state: str
    message: str

    @property
    def symbol(self) -> str:
        return {OK: "\u2713", WARN: "!", ERROR: "\u2717"}.get(self.state, "")

    @property
    def colour(self) -> str:
        return {OK: "#1a7f37", WARN: "#a05000", ERROR: "#c62828"}.get(
            self.state, "#777")


def _size(path: str) -> str:
    n = float(os.path.getsize(path))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _head(path: str):
    with open_fault_list(path) as fh:
        return [line.rstrip("\n") for line in itertools.islice(fh, SNIFF_LINES)]


def _file_basics(path: str, required: bool):
    if not path:
        return InputCheck(ERROR if required else EMPTY,
                          "required" if required else "optional — not set")
    if not os.path.exists(path):
        return InputCheck(ERROR, "file not found")
    if os.path.isdir(path):
        return InputCheck(ERROR, "this is a directory, not a file")
    if not os.access(path, os.R_OK):
        return InputCheck(ERROR, "not readable (check permissions)")
    if os.path.getsize(path) == 0:
        return InputCheck(ERROR, "file is empty")
    return None


def check_netlist(path: str) -> InputCheck:
    bad = _file_basics(path, required=True)
    if bad:
        return bad
    comp = detect_compression(path)
    try:
        head = "\n".join(_head(path))
    except (OSError, EOFError, ValueError) as exc:
        return InputCheck(ERROR, f"cannot read: {exc}")
    kind = f"{_size(path)}, {comp if comp != 'none' else 'plain text'}"
    if _MODULE_RE.search(head):
        return InputCheck(OK, f"Verilog netlist ({kind})")
    return InputCheck(WARN, f"no 'module' in the first {SNIFF_LINES} lines — "
                            f"is this a Verilog netlist? ({kind})")


def check_faults(path: str) -> InputCheck:
    bad = _file_basics(path, required=True)
    if bad:
        return bad
    comp = detect_compression(path)
    try:
        lines = _head(path)
    except (OSError, EOFError, ValueError) as exc:
        return InputCheck(ERROR, f"cannot read: {exc}")
    kind = f"{_size(path)}, {comp if comp != 'none' else 'plain text'}"
    if _is_mtfi(lines):
        return InputCheck(OK, f"Tessent MTFI fault list ({kind})")
    if any(re.search(r"\b(AU|UO|UC|DS|DI|UU|TI|BL|RE|PT|PU)\b", ln)
           for ln in lines):
        return InputCheck(OK, f"flat fault list ({kind})")
    return InputCheck(WARN, f"no fault classes seen in the first "
                            f"{SNIFF_LINES} lines ({kind})")


def check_constraints(path: str) -> InputCheck:
    bad = _file_basics(path, required=False)
    if bad:
        return bad
    return InputCheck(OK, f"constraint dofile ({_size(path)})")


def check_output_dir(path: str) -> InputCheck:
    if not path:
        return InputCheck(EMPTY, "optional — reports go next to the inputs")
    if os.path.isdir(path):
        if os.access(path, os.W_OK):
            return InputCheck(OK, "writable directory")
        return InputCheck(ERROR, "directory is not writable")
    if os.path.exists(path):
        return InputCheck(ERROR, "exists but is not a directory")
    return InputCheck(WARN, "does not exist — files are saved next to the "
                            "netlist instead")


def check_tool_reports(path: str) -> InputCheck:
    if not path:
        return InputCheck(EMPTY, "optional — report_statistics / "
                                 "analyze_fault output makes results measured")
    if os.path.isdir(path):
        n = sum(1 for name in os.listdir(path)
                if re.search(r"\.(log|txt|rpt|out)(\.gz)?$", name, re.I))
        if n:
            return InputCheck(OK, f"folder with {n} report file(s)")
        return InputCheck(WARN, "folder has no .log/.txt/.rpt/.out files")
    bad = _file_basics(path, required=True)
    if bad:
        return bad
    try:
        head = "\n".join(_head(path))
    except (OSError, EOFError, ValueError) as exc:
        return InputCheck(ERROR, f"cannot read: {exc}")
    if "Fault analysis for" in head:
        return InputCheck(OK, f"analyze_fault log ({_size(path)})")
    if re.search(r"\b(test_coverage|atpg_untestable|det_simulation)\b", head):
        return InputCheck(OK, f"report_statistics output ({_size(path)})")
    return InputCheck(WARN, "not recognised in the first lines — it is "
                            "still searched in full")


CHECKS = {
    "netlist": check_netlist,
    "faults": check_faults,
    "constraints": check_constraints,
    "outdir": check_output_dir,
    "tool_reports": check_tool_reports,
}

_CONSTRAINT_RE = re.compile(
    r"^\s*(add_|set_|delete_|report_|dofile|source|read_|create_|"
    r"set_context|add_input_constraints|add_clocks)", re.MULTILINE)
_CONSTRAINT_EXT = (".do", ".dofile", ".tcl")


def classify_file(path: str):
    """What a dropped path is: netlist, faults, constraints, report, outdir.

    ``None`` when it is none of them. Netlists and fault lists are recognised
    by content, so a renamed file still lands in the right box.
    """
    if os.path.isdir(path):
        return "outdir"
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        return None
    lower = path.lower()
    if lower.endswith(".json"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            return None
        from ..reporting.session_report import FORMAT_MARKER
        return "report" if FORMAT_MARKER in head else None
    if check_netlist(path).state == OK:
        return "netlist"
    if lower.endswith(_CONSTRAINT_EXT):
        return "constraints"
    if check_faults(path).state == OK:
        return "faults"
    try:
        head = "\n".join(_head(path))
    except (OSError, EOFError, ValueError):
        return None
    if _CONSTRAINT_RE.search(head):
        return "constraints"
    return None
