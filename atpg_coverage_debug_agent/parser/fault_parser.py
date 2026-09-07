"""Parser for Tessent-style ATPG fault lists.

Two on-disk shapes are supported:

1. **Tessent MTFI structured format** (``*.faults.mtfi`` / ``.mtfi.gz``).
   The file *declares its own layout*, and this parser reads that declaration
   rather than assuming one::

       FaultInformation {
        version : 1;
        FaultType (Stuck) {
         FaultList {
          FaultCollapsing : FALSE;
          Format : Identifier, Class, Location;
          Instance ("") {
             0,  AU.SEQ,    "/path/to/instance/pin";
             1,  TI,        "/path/to/instance/pin";

   The ``Format :`` line names the columns, so a file that declares a
   different field order parses correctly with no code change. ``FaultType``,
   ``version`` and ``FaultCollapsing`` are propagated into
   :class:`FaultListHeader` because a collapsed and an uncollapsed census mean
   different things and a report that does not say which is unreadable.

2. **Flat whitespace format** (legacy / simple lists)::

       <class> <stuck_value> <object_path>     e.g. AU 1 /core/u_alu/U12/Y
       <object_path> <class>                   e.g. /core/u_alu/U12/Y AU
       <class> <object_path>                   e.g. UO top/reg_bank/U5/Q

Two rules matter more than the formats themselves:

* **The class is read by FIELD POSITION, never by substring search.** A
  ``Location`` is a free-form hierarchical path and collides with short class
  tokens: on one real partition a substring count of "AU" over the file
  overstated the AU population by around eight percent, purely because that
  many instance paths happened to contain those two letters. The same trap
  exists for ``DS``, ``DI``, ``TI``, ``RE``, ``BL``, ``PT`` and ``PU`` on any
  design.
* **An unrecognised class is never swept into a catch-all.** Each distinct
  unknown token is warned about by name, sampled verbatim and counted; past a
  configurable threshold the parse fails outright. See :mod:`..diagnostics`.

Dotted subtypes are preserved verbatim in ``raw_class_token`` while the coarse
:class:`FaultClass` is derived from the prefix before the first ``.``.
"""

from __future__ import annotations

import bz2
import gzip
import itertools
import logging
import lzma
import re
from dataclasses import dataclass, field
from typing import (Any, Dict, IO, Iterable, Iterator, List, Optional, Set,
                    Tuple, Union)

from ..config.analysis_config import AnalysisConfig, resolve
from ..diagnostics import UnrecognisedReport, UnrecognisedTracker
from ..models import FaultClass, FaultRecord

logger = logging.getLogger(__name__)

_COMMENT_PREFIXES = ("//", "#", "*", ";")
# A path-like token contains a hierarchy separator or looks like an identifier.
_PATH_LIKE = re.compile(r"[A-Za-z0-9_$]")
_HIER_SEP = re.compile(r"[\/\.]")
_STUCK_VALUE = re.compile(r"^(?:sa)?[01]$", re.IGNORECASE)

#: How many leading lines are buffered to decide the on-disk format. Bounded so
#: a multi-gigabyte fault list is still streamed rather than read into memory.
HEADER_PROBE_LINES = 400

#: Column order assumed only when a file declares no ``Format :`` line.
DEFAULT_MTFI_FORMAT = ("identifier", "class", "location")

# ``Name : value;`` header assignment, e.g. ``FaultCollapsing : FALSE;``.
_MTFI_KEYWORD = re.compile(r"^\s*([A-Za-z][\w ]*?)\s*:\s*(.*?)\s*;?\s*$")
# ``BlockName (argument) {`` or ``BlockName {`` block opener.
_MTFI_BLOCK = re.compile(r"^\s*([A-Za-z][\w]*)\s*(?:\(\s*(.*?)\s*\))?\s*\{\s*$")

#: Magic-byte signatures. Compression is detected from CONTENT, not from the
#: file name: a real partition shipped plain ASCII named ``.faults.mtfi.gz``,
#: and every extension-based reader failed on it.
_MAGIC_OPENERS: Tuple[Tuple[bytes, Any, str], ...] = (
    (b"\x1f\x8b", gzip.open, "gzip"),
    (b"BZh", bz2.open, "bzip2"),
    (b"\xfd7zXZ\x00", lzma.open, "xz"),
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


# ---------------------------------------------------------------------------
# Parse results
# ---------------------------------------------------------------------------
@dataclass
class FaultListHeader:
    """What the fault list said about itself.

    Attributes:
        file_format: ``"mtfi"`` or ``"flat"``.
        compression: ``"none"``, ``"gzip"``, ``"bzip2"`` or ``"xz"``, decided
            from magic bytes.
        version: The ``version :`` value, when declared.
        fault_collapsing: ``True``/``False`` from ``FaultCollapsing :``, or
            ``None`` when the file did not say. A count means something
            different under each, so this is reported rather than assumed.
        fault_models: Every ``FaultType (...)`` block encountered, in order.
        format_fields: Column names from the last ``Format :`` line.
        declared_formats: Every distinct ``Format :`` declaration seen.
        per_model_counts: ``{fault model: {class token: count}}`` so a file
            carrying several fault models reports a census per model instead
            of one meaningless total.
        instance_prefixes: ``Instance (...)`` arguments seen, in order.
    """

    file_format: str = "flat"
    compression: str = "none"
    version: Optional[str] = None
    fault_collapsing: Optional[bool] = None
    fault_models: List[str] = field(default_factory=list)
    format_fields: List[str] = field(default_factory=list)
    declared_formats: List[List[str]] = field(default_factory=list)
    per_model_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    instance_prefixes: List[str] = field(default_factory=list)

    @property
    def collapsing_label(self) -> str:
        """Human-readable collapsing state for report headers."""
        if self.fault_collapsing is None:
            return "not declared by the fault list"
        return "collapsed" if self.fault_collapsing else "uncollapsed"

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "file_format": self.file_format,
            "compression": self.compression,
            "version": self.version,
            "fault_collapsing": self.fault_collapsing,
            "collapsing_label": self.collapsing_label,
            "fault_models": list(self.fault_models),
            "format_fields": list(self.format_fields),
            "declared_formats": [list(f) for f in self.declared_formats],
            "per_model_counts": {k: dict(v)
                                 for k, v in self.per_model_counts.items()},
            "instance_prefixes": list(self.instance_prefixes),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]
                  ) -> Optional["FaultListHeader"]:
        """Rebuild a header from :meth:`as_dict`, or ``None`` when absent."""
        if not data:
            return None
        return cls(
            file_format=str(data.get("file_format", "flat")),
            compression=str(data.get("compression", "none")),
            version=data.get("version"),
            fault_collapsing=data.get("fault_collapsing"),
            fault_models=list(data.get("fault_models") or []),
            format_fields=list(data.get("format_fields") or []),
            declared_formats=[list(f)
                              for f in (data.get("declared_formats") or [])],
            per_model_counts={k: dict(v) for k, v in
                              (data.get("per_model_counts") or {}).items()},
            instance_prefixes=list(data.get("instance_prefixes") or []),
        )


@dataclass
class FaultListParseResult:
    """Everything one fault-list parse produced.

    Attributes:
        records: Parsed fault records, in file order.
        warnings: Human-readable warnings, including one per distinct
            unrecognised class token.
        header: What the file declared about itself.
        unrecognised: Per-token accounting of classes the tool does not know.
        total_rows: Data rows recognised as records, i.e. the population every
            census must reconcile against.
    """

    records: List[FaultRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    header: FaultListHeader = field(default_factory=FaultListHeader)
    unrecognised: Optional[UnrecognisedReport] = None
    total_rows: int = 0

    def as_tuple(self) -> Tuple[List[FaultRecord], List[str]]:
        """Backwards-compatible ``(records, warnings)`` view."""
        return self.records, self.warnings


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------
def detect_compression(path: str) -> str:
    """Return ``none``/``gzip``/``bzip2``/``xz`` for *path* from magic bytes."""
    try:
        with open(path, "rb") as probe:
            head = probe.read(8)
    except OSError:
        return "none"
    for magic, _opener, label in _MAGIC_OPENERS:
        if head.startswith(magic):
            return label
    return "none"


def open_fault_list(path: str) -> IO[str]:
    """Open *path* as text, decompressing based on magic bytes.

    File names lie. One partition shipped an uncompressed ASCII fault list
    named ``*.faults.mtfi.gz``; sniffing the content is the only reliable way
    to open it, and the same applies to a gzip stream saved without a suffix.
    """
    with open(path, "rb") as probe:
        head = probe.read(8)
    for magic, opener, _label in _MAGIC_OPENERS:
        if head.startswith(magic):
            return opener(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _iter_lines(source: Union[str, Iterable[str]]) -> Iterator[str]:
    if isinstance(source, str):
        return iter(source.splitlines())
    return (line.rstrip("\n") for line in source)


def _is_mtfi(sample_lines: List[str]) -> bool:
    """Decide whether the buffered head of a file is Tessent MTFI."""
    head = "\n".join(sample_lines[:HEADER_PROBE_LINES])
    return (
        "FaultInformation" in head
        or "FaultList" in head
        or re.search(r"^\s*Format\s*:", head, re.MULTILINE) is not None
    )


def _split_fields(body: str) -> List[str]:
    """Split one MTFI data row on commas that are outside quotes."""
    out: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for ch in body:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == ",":
            out.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    out.append("".join(current).strip())
    return out


# ---------------------------------------------------------------------------
# Class-token handling
# ---------------------------------------------------------------------------
def _coarse_class(token: str) -> FaultClass:
    """Map a (possibly dotted) class token to a coarse :class:`FaultClass`.

    ``AU.TC`` -> ``AU``, ``DI.CLK`` -> ``DI``, ``UO.AAB`` -> ``UO``. A family
    this build does not model maps to ``UNKNOWN``; the caller keeps the full
    token in ``raw_class_token`` and reports it as unrecognised, so nothing is
    lost and nothing is silently reclassified.
    """
    base = token.split(".", 1)[0]
    return FaultClass.from_token(base)


def _known_class_tokens(config: AnalysisConfig) -> frozenset:
    """Class families recognised in the flat format.

    Sourced from the configurable role map as well as the enum, so a class
    onboarded through configuration is recognised in flat lists too and does
    not require the enum to be extended.
    """
    tokens = {c.value for c in FaultClass if c is not FaultClass.UNKNOWN}
    tokens.update(config.known_families())
    return frozenset(tokens)


def _census(header: FaultListHeader, model: str, token: str) -> None:
    bucket = header.per_model_counts.setdefault(model or "(unspecified)", {})
    bucket[token] = bucket.get(token, 0) + 1


# ---------------------------------------------------------------------------
# Flat whitespace format
# ---------------------------------------------------------------------------
def _parse_line(line: str, line_number: int,
                known_tokens: Optional[frozenset] = None
                ) -> Optional[FaultRecord]:
    """Parse one flat-format line, or ``None`` when it holds no fault."""
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped:
        return None
    if any(stripped.startswith(p) for p in _COMMENT_PREFIXES):
        return None

    if known_tokens is None:
        known_tokens = _known_class_tokens(resolve(None))

    # Drop a leading equivalence marker like '--' or '-' used by some tools.
    work = re.sub(r"^[-=>+\s]+", "", stripped)
    tokens = work.split()
    if not tokens:
        return None

    fault_class = FaultClass.UNKNOWN
    raw_class_token = ""
    class_index = -1
    for idx, tok in enumerate(tokens):
        # A WHOLE token must equal a class family (optionally dotted). A path
        # that merely contains "AU" is not a class and must never read as one.
        base = tok.split(".", 1)[0]
        if "/" in base:
            continue
        if base.upper() in known_tokens:
            fault_class = FaultClass.from_token(base)
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

    if fault_class is FaultClass.UNKNOWN and not raw_class_token:
        # Nothing on the line looked like a class. Keep the leading token so
        # the unrecognised report can name it rather than say "unknown".
        first = tokens[0]
        raw_class_token = first if first != fault_object else ""

    return FaultRecord(
        raw_text=raw,
        line_number=line_number,
        fault_object=fault_object,
        normalized_object=normalize_object(fault_object),
        fault_class=fault_class,
        raw_class_token=raw_class_token,
        fault_type=fault_type,
    )


def _parse_flat(lines: Iterator[str], config: AnalysisConfig,
                result: FaultListParseResult,
                tracker: UnrecognisedTracker) -> None:
    """Parse the flat whitespace format into *result*."""
    known_tokens = _known_class_tokens(config)
    result.header.file_format = "flat"
    for i, line in enumerate(lines, start=1):
        try:
            record = _parse_line(line, i, known_tokens)
        except Exception as exc:  # defensive: never abort the whole parse
            result.warnings.append(f"Line {i}: failed to parse ({exc}).")
            continue
        if record is None:
            continue
        result.records.append(record)
        result.total_rows += 1
        tracker.seen()
        token = record.raw_class_token or record.fault_class.value
        _census(result.header, "", token)
        if not config.is_known_class(record.dotted_class):
            tracker.add(token, sample=line.strip(), line_number=i)


# ---------------------------------------------------------------------------
# Tessent MTFI structured format
# ---------------------------------------------------------------------------
def _parse_mtfi(lines: Iterator[str], config: AnalysisConfig,
                result: FaultListParseResult,
                tracker: UnrecognisedTracker) -> None:
    """Parse the Tessent MTFI structured format, driven by its own header."""
    header = result.header
    header.file_format = "mtfi"
    known_tokens = _known_class_tokens(config)

    format_fields: List[str] = []
    current_model = ""
    instance_prefix = ""
    shape_warned: Set[Tuple[int, int]] = set()
    no_format_warned = False

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped in ("{", "}"):
            continue
        if stripped.startswith("//"):
            continue

        block = _MTFI_BLOCK.match(stripped)
        if block:
            name, arg = block.group(1), (block.group(2) or "").strip()
            lowered = name.lower()
            if lowered == "faulttype":
                current_model = arg.strip('"').strip() or "(unspecified)"
                if current_model not in header.fault_models:
                    header.fault_models.append(current_model)
            elif lowered == "instance":
                instance_prefix = arg.strip('"').strip()
                if instance_prefix not in header.instance_prefixes:
                    header.instance_prefixes.append(instance_prefix)
            continue

        # A header assignment has its colon before any comma; a data row does
        # not. Checking that order is what stops ``Format : a, b, c;`` being
        # mistaken for a three-field data row.
        colon = stripped.find(":")
        comma = stripped.find(",")
        if colon != -1 and (comma == -1 or colon < comma):
            kw = _MTFI_KEYWORD.match(stripped)
            if kw:
                key, value = kw.group(1).strip().lower(), kw.group(2).strip()
                if key == "format":
                    format_fields = [f.strip().lower()
                                     for f in value.split(",") if f.strip()]
                    header.format_fields = list(format_fields)
                    if format_fields not in header.declared_formats:
                        header.declared_formats.append(list(format_fields))
                elif key == "version":
                    header.version = value
                elif key == "faultcollapsing":
                    header.fault_collapsing = value.strip().upper() == "TRUE"
                continue

        if not stripped.endswith(";"):
            continue

        fields_out = _split_fields(stripped[:-1])
        if len(fields_out) < 2:
            continue

        active_format = format_fields or list(DEFAULT_MTFI_FORMAT)
        if not format_fields and not no_format_warned:
            no_format_warned = True
            result.warnings.append(
                "MTFI fault list declared no 'Format :' line; assuming the "
                f"default column order {', '.join(DEFAULT_MTFI_FORMAT)}."
            )
        if len(fields_out) != len(active_format):
            shape = (len(fields_out), len(active_format))
            if shape not in shape_warned:
                shape_warned.add(shape)
                result.warnings.append(
                    f"Line {i}: data row has {len(fields_out)} field(s) but "
                    f"'Format :' declares {len(active_format)} "
                    f"({', '.join(active_format)}). Fields are matched "
                    f"positionally as far as they go."
                )

        values = dict(zip(active_format, fields_out))
        token = (values.get("class") or "").strip()
        location = (values.get("location") or "").strip()
        identifier = (values.get("identifier") or "").strip()
        if not token or not location:
            continue

        if instance_prefix:
            location = f"{instance_prefix.rstrip('/')}/{location.lstrip('/')}"

        record = FaultRecord(
            raw_text=stripped,
            line_number=i,
            fault_object=location,
            normalized_object=normalize_object(location),
            fault_class=_coarse_class(token),
            raw_class_token=token,
            fault_type=identifier if identifier in ("0", "1") else None,
        )
        result.records.append(record)
        result.total_rows += 1
        tracker.seen()
        _census(header, current_model, token)

        if (config.family_of(token) not in known_tokens
                and not config.is_known_class(token)):
            tracker.add(token, sample=stripped, line_number=i)

    if not result.records:
        result.warnings.append(
            "MTFI fault list recognised but no data rows matched the declared "
            "'Format :' columns."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_fault_list_ex(source: Union[str, Iterable[str]],
                        config: Optional[AnalysisConfig] = None,
                        enforce: bool = False) -> FaultListParseResult:
    """Parse a fault list from text or a line iterator.

    Args:
        source: Fault-list text, or any iterable of lines (a file object
            works, and keeps the parse streaming).
        config: Analysis configuration; the active one is used when omitted.
        enforce: Raise :class:`..diagnostics.ThresholdExceeded` when the share
            of unrecognised classes is above the configured threshold. The
            file-level entry point enables this; the text-level one does not,
            so a unit test can inspect a deliberately malformed input.

    Returns:
        A :class:`FaultListParseResult`.
    """
    config = resolve(config)
    result = FaultListParseResult()
    tracker = UnrecognisedTracker(
        domain="fault class",
        threshold_pct=config.unknown_class_threshold_pct,
        sample_limit=config.sample_limit,
        fatal=config.unknown_class_fatal,
    )

    lines = _iter_lines(source)
    probe = list(itertools.islice(lines, HEADER_PROBE_LINES))
    stream = itertools.chain(probe, lines)

    if _is_mtfi(probe):
        _parse_mtfi(stream, config, result, tracker)
    else:
        _parse_flat(stream, config, result, tracker)

    result.unrecognised = tracker.enforce() if enforce else tracker.report()
    result.warnings.extend(result.unrecognised.warnings())
    logger.info(
        "Parsed %d %s fault record(s); %d unrecognised-class record(s) across "
        "%d distinct token(s); %d warning(s).",
        len(result.records), result.header.file_format,
        result.unrecognised.count, len(result.unrecognised.tokens),
        len(result.warnings),
    )
    return result


def parse_fault_list(text: str, config: Optional[AnalysisConfig] = None
                     ) -> Tuple[List[FaultRecord], List[str]]:
    """Parse fault-list *text*.

    The Tessent MTFI structured format is detected automatically; otherwise the
    flat whitespace format is used.

    Returns:
        A tuple ``(records, warnings)``. Use :func:`parse_fault_list_ex` when
        the declared header or the unrecognised-class accounting is needed.
    """
    return parse_fault_list_ex(text, config=config, enforce=False).as_tuple()


def parse_fault_list_file_ex(path: str,
                             config: Optional[AnalysisConfig] = None,
                             enforce: bool = True) -> FaultListParseResult:
    """Stream *path* and parse it as a fault list.

    Compression is detected from magic bytes, so the file name never decides
    how the file is read. The file is streamed line by line: real fault lists
    exceed a gigabyte and must not be loaded into memory.
    """
    compression = detect_compression(path)
    with open_fault_list(path) as handle:
        result = parse_fault_list_ex(handle, config=config, enforce=enforce)
    result.header.compression = compression
    return result


def parse_fault_list_file(path: str, config: Optional[AnalysisConfig] = None
                          ) -> Tuple[List[FaultRecord], List[str]]:
    """Read *path* (compression auto-detected) and parse it as a fault list."""
    return parse_fault_list_file_ex(path, config=config).as_tuple()
