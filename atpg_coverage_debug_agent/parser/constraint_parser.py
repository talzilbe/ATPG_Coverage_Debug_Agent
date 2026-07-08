"""Parser for a practical, human-readable constraint file format.

Real ATPG constraint files come in many dialects (Tessent ``add_input_constraints``
style commands, dofile snippets, custom CSVs, ...). Rather than locking onto a
single vendor syntax we detect *intent* by keyword. Recognised line shapes:

* ``force <signal> <value>``            -> forced value
* ``constant <signal> <value>``         -> tied constant
* ``tie <signal> <value>``              -> tied constant
* ``disable <path>``                    -> disabled path
* ``block <signal>``                    -> blocked control
* ``constrain <signal> <value>``        -> constrained port
* ``clock <signal>`` / ``reset <signal>`` / ``test_en <signal>`` / ``scan_en``
* Tessent-like ``add_input_constraints <signal> C0|C1|CX``
* ``set_atpg_constraint`` / ``add_atpg_constraints`` lines

Anything not matched is preserved as a ``kind='unknown'`` record with a warning
so no information is lost.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from ..models import ConstraintRecord
from .fault_parser import normalize_object

logger = logging.getLogger(__name__)

_COMMENT_PREFIXES = ("//", "#", ";")

# Keyword -> canonical constraint kind.
_KIND_KEYWORDS = {
    "force": "force",
    "constant": "constant",
    "tie": "constant",
    "tied": "constant",
    "disable": "disable",
    "disabled": "disable",
    "block": "block",
    "blocked": "block",
    "constrain": "constrain",
    "constraint": "constrain",
    "constraints": "constrain",
    "clock": "clock",
    "reset": "reset",
    "test_en": "test_enable",
    "test_enable": "test_enable",
    "scan_en": "test_enable",
    "scan_enable": "test_enable",
    "add_input_constraints": "constrain",
    "add_atpg_constraints": "constrain",
    "set_atpg_constraint": "constrain",
}

# Tessent constraint value codes -> logical value.
_VALUE_CODES = {
    "c0": "0", "c1": "1", "cx": "X",
    "0": "0", "1": "1", "x": "X",
}

_SIGNAL_TOKEN = re.compile(r"[A-Za-z0-9_$\/\.\[\]]+")


def _extract_value(tokens: List[str]) -> Optional[str]:
    for tok in tokens:
        low = tok.lower()
        if low in _VALUE_CODES:
            return _VALUE_CODES[low]
    return None


def _extract_signal(tokens: List[str], kind_index: int) -> Optional[str]:
    """Pick the first path-like token after the keyword as the signal."""
    for tok in tokens[kind_index + 1:]:
        if tok.lower() in _VALUE_CODES:
            continue
        if _SIGNAL_TOKEN.fullmatch(tok):
            return tok
    return None


def _parse_line(line: str, line_number: int) -> Optional[ConstraintRecord]:
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped:
        return None
    if any(stripped.startswith(p) for p in _COMMENT_PREFIXES):
        return None

    # Allow simple "key = value" assignment style too.
    assign = re.match(r"^(?P<sig>[\w$\/\.\[\]]+)\s*=\s*(?P<val>[01xX])\s*$",
                      stripped)
    if assign:
        sig = assign.group("sig")
        return ConstraintRecord(
            raw_text=raw,
            line_number=line_number,
            kind="constant",
            signal=sig,
            normalized_signal=normalize_object(sig),
            value=assign.group("val").upper().replace("X", "X"),
            notes="parsed from assignment syntax",
        )

    tokens = re.split(r"[\s,()]+", stripped)
    tokens = [t for t in tokens if t]
    if not tokens:
        return None

    kind = None
    kind_index = -1
    for idx, tok in enumerate(tokens):
        low = tok.lower()
        if low in _KIND_KEYWORDS:
            kind = _KIND_KEYWORDS[low]
            kind_index = idx
            break

    if kind is None:
        return ConstraintRecord(
            raw_text=raw,
            line_number=line_number,
            kind="unknown",
            signal=None,
            normalized_signal=None,
            notes="no recognised constraint keyword",
        )

    signal = _extract_signal(tokens, kind_index)
    value = _extract_value(tokens)
    return ConstraintRecord(
        raw_text=raw,
        line_number=line_number,
        kind=kind,
        signal=signal,
        normalized_signal=normalize_object(signal) if signal else None,
        value=value,
    )


def parse_constraints(text: str) -> Tuple[List[ConstraintRecord], List[str]]:
    """Parse constraint *text*.

    Returns:
        ``(records, warnings)``.
    """
    records: List[ConstraintRecord] = []
    warnings: List[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        try:
            record = _parse_line(line, i)
        except Exception as exc:  # defensive
            warnings.append(f"Line {i}: failed to parse constraint ({exc}).")
            continue
        if record is None:
            continue
        if record.kind == "unknown":
            warnings.append(
                f"Line {i}: could not classify constraint '{line.strip()}'."
            )
        records.append(record)
    logger.info("Parsed %d constraint record(s) with %d warning(s).",
                len(records), len(warnings))
    return records, warnings


def parse_constraints_file(path: str) -> Tuple[List[ConstraintRecord], List[str]]:
    """Read *path* and parse it as constraints."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_constraints(handle.read())
