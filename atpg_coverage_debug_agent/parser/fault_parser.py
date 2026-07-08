"""Parser for Tessent-style ATPG fault lists.

Two on-disk shapes are supported:

1. **Tessent MTFI structured format** (``*.faults.mtfi`` / ``.mtfi.gz``).
   The file is a brace-delimited block whose data rows follow the column
   order declared by a ``Format :`` line, e.g.::

       FaultInformation {
        FaultType (Stuck) {
         FaultList {
          Format : Identifier, Class, Location;
          Instance ("") {
             0,  AU,        "/top/u_clkgate/optlc_125/o";
             1,  AU.TC,     "/top/u_seq/optlc_900/o";
             0,  DS,        "/top/u_xyz/o";

   Fields are comma separated: the *Identifier* is the stuck value (0/1), the
   *Class* may carry a dotted subtype (``AU.TC``, ``DI.CLK``, ``UO.AAB`` ...)
   and the *Location* is a quoted hierarchical path.

2. **Flat whitespace format** (legacy / simple lists)::

       <class> <stuck_value> <object_path>     e.g. AU 1 /core/u_alu/U12/Y
       <object_path> <class>                   e.g. /core/u_alu/U12/Y AU
       <class> <object_path>                   e.g. UO top/reg_bank/U5/Q

Dotted subtypes are preserved verbatim in ``raw_class_token`` while the coarse
:class:`FaultClass` is derived from the prefix before the first ``.``.
"""

from __future__ import annotations

import gzip
import logging
import re
from typing import List, Optional, Tuple

from ..models import FaultClass, FaultRecord

logger = logging.getLogger(__name__)

_COMMENT_PREFIXES = ("//", "#", "*", ";")
# A path-like token contains a hierarchy separator or looks like an identifier.
_PATH_LIKE = re.compile(r"[A-Za-z0-9_$]")
_HIER_SEP = re.compile(r"[\/\.]")
_STUCK_VALUE = re.compile(r"^(?:sa)?[01]$", re.IGNORECASE)

# Known single-letter / two-letter class tokens we recognise.
_KNOWN_CLASS_TOKENS = {c.value for c in FaultClass if c is not FaultClass.UNKNOWN}

# A single MTFI data row: ``<id>, <CLASS[.SUB]>, "<location>";``
_MTFI_DATA = re.compile(
    r'^\s*([0-9])\s*,\s*([A-Za-z][\w.]*)\s*,\s*"?([^";]+?)"?\s*;?\s*$'
)


def normalize_object(obj: str) -> str:
    """Normalise a fault object string for robust matching.

    The normalisation:

    * strips surrounding whitespace and quotes
    * converts ``.`` hierarchy separators to ``/``
    * collapses repeated separators
    * removes a single leading separator
    * lower-cases nothing (names are case sensitive in Verilog)
    """
    obj = obj.strip().strip('"').strip("'")
    obj = obj.replace("\\", "")
    obj = _HIER_SEP.sub("/", obj)
    obj = re.sub(r"/+", "/", obj)
    obj = obj.lstrip("/")
    return obj


def _looks_like_path(token: str) -> bool:
    return bool(_PATH_LIKE.search(token)) and not _STUCK_VALUE.match(token)


def _parse_line(line: str, line_number: int) -> Optional[FaultRecord]:
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped:
        return None
    if any(stripped.startswith(p) for p in _COMMENT_PREFIXES):
        return None

    # Drop a leading equivalence marker like '--' or '-' used by some tools.
    work = re.sub(r"^[-=>+\s]+", "", stripped)
    tokens = work.split()
    if not tokens:
        return None

    fault_class = FaultClass.UNKNOWN
    raw_class_token = ""
    class_index = -1
    for idx, tok in enumerate(tokens):
        if tok.upper() in _KNOWN_CLASS_TOKENS:
            fault_class = FaultClass.from_token(tok)
            raw_class_token = tok
            class_index = idx
            break

    # Identify the stuck-at value token if present.
    fault_type: Optional[str] = None
    for tok in tokens:
        if _STUCK_VALUE.match(tok):
            fault_type = tok[-1]
            break

    # The fault object is the longest path-like token that is not the class.
    candidates: List[Tuple[int, str]] = []
    for idx, tok in enumerate(tokens):
        if idx == class_index:
            continue
        if _STUCK_VALUE.match(tok):
            continue
        if _looks_like_path(tok):
            score = len(tok) + (5 if _HIER_SEP.search(tok) else 0)
            candidates.append((score, tok))
    if not candidates:
        return None
    fault_object = max(candidates, key=lambda c: c[0])[1]

    return FaultRecord(
        raw_text=raw,
        line_number=line_number,
        fault_object=fault_object,
        normalized_object=normalize_object(fault_object),
        fault_class=fault_class,
        raw_class_token=raw_class_token,
        fault_type=fault_type,
    )


def _coarse_class(token: str) -> FaultClass:
    """Map a (possibly dotted) class token to a coarse :class:`FaultClass`.

    ``AU.TC`` -> ``AU``, ``DI.CLK`` -> ``DI``, ``UO.AAB`` -> ``UO``.  Tokens
    with no coarse equivalent (e.g. ``UU`` unused) map to ``UNKNOWN`` but the
    full token is still preserved by the caller in ``raw_class_token``.
    """
    base = token.split(".", 1)[0]
    return FaultClass.from_token(base)


def _is_mtfi(text: str) -> bool:
    """Heuristically decide whether *text* is a Tessent MTFI structured file."""
    head = text[:4096]
    return (
        "FaultInformation" in head
        or "Format :" in head
        or "FaultList" in head
    )


def _parse_mtfi(text: str) -> Tuple[List[FaultRecord], List[str]]:
    """Parse the Tessent MTFI structured fault-list format."""
    records: List[FaultRecord] = []
    warnings: List[str] = []
    match = _MTFI_DATA.match
    for i, line in enumerate(text.splitlines(), start=1):
        m = match(line)
        if m is None:
            continue  # structural / header / brace line - silently skipped
        stuck, token, location = m.group(1), m.group(2), m.group(3)
        location = location.strip()
        if not location:
            continue
        records.append(FaultRecord(
            raw_text=line.rstrip("\n"),
            line_number=i,
            fault_object=location,
            normalized_object=normalize_object(location),
            fault_class=_coarse_class(token),
            raw_class_token=token,
            fault_type=stuck,
        ))
    if not records:
        warnings.append(
            "MTFI fault list recognised but no data rows matched the "
            "expected '<id>, <class>, \"<location>\";' pattern."
        )
    logger.info("Parsed %d MTFI fault record(s) with %d warning(s).",
                len(records), len(warnings))
    return records, warnings


def parse_fault_list(text: str) -> Tuple[List[FaultRecord], List[str]]:
    """Parse fault-list *text*.

    The Tessent MTFI structured format is detected automatically; otherwise the
    flat whitespace format is used.

    Returns:
        A tuple ``(records, warnings)``.
    """
    if _is_mtfi(text):
        return _parse_mtfi(text)

    records: List[FaultRecord] = []
    warnings: List[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        try:
            record = _parse_line(line, i)
        except Exception as exc:  # defensive: never abort whole parse
            warnings.append(f"Line {i}: failed to parse ({exc}).")
            continue
        if record is None:
            continue
        if record.fault_class is FaultClass.UNKNOWN:
            warnings.append(
                f"Line {i}: unrecognised fault class in '{line.strip()}'."
            )
        records.append(record)
    logger.info("Parsed %d fault record(s) with %d warning(s).",
                len(records), len(warnings))
    return records, warnings


def _read_text(path: str) -> str:
    """Read *path* as text, transparently decompressing gzip files."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def parse_fault_list_file(path: str) -> Tuple[List[FaultRecord], List[str]]:
    """Read *path* (optionally gzip-compressed) and parse it as a fault list."""
    return parse_fault_list(_read_text(path))
