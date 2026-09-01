"""A pragmatic structural parser for gate-level hierarchical Verilog.

This is **not** a full Verilog front-end. It recognises the common structural
subset emitted by synthesis/DFT tools:

* ``module <name> ( <ports> );`` ... ``endmodule`` blocks
* ``input`` / ``output`` / ``inout`` / ``wire`` / ``reg`` declarations
* Instantiations of the form
  ``CELLTYPE inst_name ( .PIN(net), .PIN(net) );``
* Positional instantiations ``CELLTYPE inst_name ( a, b, y );`` (best effort)

Documented assumptions & limitations are listed at the bottom of this module
and surfaced to the user via parser warnings.
"""

from __future__ import annotations

import bisect
import gzip
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..models import Instance, Module, Net, Pin

logger = logging.getLogger(__name__)


# Regexes for the structural subset we support. They are intentionally lenient.
_COMMENT_LINE = re.compile(r"//.*?$", re.MULTILINE)
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_MODULE_RE = re.compile(
    r"\bmodule\s+(?P<name>\\?[\w$\\\[\].]+)\s*(?P<ports>\([^;]*?\))?\s*;",
    re.DOTALL,
)
_ENDMODULE_RE = re.compile(r"\bendmodule\b")
_PORT_DECL_RE = re.compile(
    r"\b(?P<dir>input|output|inout)\b\s*(?:wire|reg)?\s*"
    r"(?P<range>\[[^\]]*\])?\s*(?P<names>[^;]+);",
    re.MULTILINE,
)
# An instantiation: TYPE name ( ... );  (TYPE is not a Verilog keyword)
_INSTANCE_RE = re.compile(
    r"(?P<type>\\?[\w$]+)\s+(?P<name>\\?[\w$\\\[\]]+)\s*"
    r"\((?P<conns>.*?)\)\s*;",
    re.DOTALL,
)
_NAMED_CONN_RE = re.compile(r"\.(?P<pin>\\?[\w$]+)\s*\(\s*(?P<net>[^)]*?)\s*\)")

# Verilog keywords that must never be treated as a cell type when scanning
# for instantiations.
_VERILOG_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg",
    "assign", "parameter", "localparam", "always", "initial", "begin",
    "end", "generate", "endgenerate", "supply0", "supply1", "tri", "wand",
    "wor", "function", "endfunction", "task", "endtask", "specify",
    "endspecify", "defparam", "genvar", "for", "if", "else", "case",
    "endcase", "default",
}

# ---------------------------------------------------------------------------
# Scan pin conventions
# ---------------------------------------------------------------------------
# A cell is scannable when it has BOTH a dedicated scan-data input and a
# shift-enable pin. These sets are the vocabulary for that test; matching is on
# the literal pin name of an instantiation, never on the instance name.
_SCAN_IN_PINS = frozenset({"si", "sd", "ti", "sin", "scan_in", "scanin",
                           "sdi", "test_si", "tie"})
_SCAN_OUT_PINS = frozenset({"so", "to", "sout", "scan_out", "scanout",
                            "sdo", "test_so", "q_so"})
_SHIFT_ENABLE_PINS = frozenset({"se", "ssb", "sen", "scan_en", "scan_enable",
                                "shift_en", "test_se", "sh", "smc"})


def scan_pin_role(pin_name: str) -> Optional[str]:
    """Return ``'scan_in'`` / ``'scan_out'`` / ``'shift_enable'`` or ``None``.

    Args:
        pin_name: The literal pin name from an instantiation.
    """
    name = (pin_name or "").strip().lstrip(".").lower()
    if name in _SCAN_IN_PINS:
        return "scan_in"
    if name in _SCAN_OUT_PINS:
        return "scan_out"
    if name in _SHIFT_ENABLE_PINS:
        return "shift_enable"
    return None


@dataclass
class VerilogNetlist:
    """Container for a parsed netlist plus elaboration helpers."""

    modules: Dict[str, Module] = field(default_factory=dict)
    top_module: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    # -- elaboration ------------------------------------------------------
    def infer_top(self) -> Optional[str]:
        """Infer the top module as the one never instantiated elsewhere."""
        if not self.modules:
            return None
        instantiated = set()
        for module in self.modules.values():
            for inst in module.instances.values():
                instantiated.add(inst.cell_type)
        candidates = [name for name in self.modules if name not in instantiated]
        if len(candidates) == 1:
            self.top_module = candidates[0]
        elif candidates:
            # Pick the most complex candidate as a heuristic.
            self.top_module = max(
                candidates, key=lambda n: len(self.modules[n].instances)
            )
        else:
            self.top_module = next(iter(self.modules))
        return self.top_module

    def all_instances(self) -> List[Tuple[str, Instance]]:
        """Return ``(module_name, instance)`` for every instance parsed."""
        out: List[Tuple[str, Instance]] = []
        for mod_name, module in self.modules.items():
            for inst in module.instances.values():
                out.append((mod_name, inst))
        return out


def _blank(match: "re.Match") -> str:
    """Replace matched text with its own newlines so line numbers survive."""
    return "\n" * match.group(0).count("\n")


def _strip_comments(text: str) -> str:
    # Blank comments out rather than deleting them: every reported line number
    # is meant to be pasted into an editor, so offsets must not drift.
    text = _COMMENT_BLOCK.sub(_blank, text)
    text = _COMMENT_LINE.sub("", text)
    return text


class _LineIndex:
    """Character-offset -> 1-based line number, cheap for large netlists."""

    def __init__(self, text: str) -> None:
        self._newlines: List[int] = []
        pos = text.find("\n")
        while pos != -1:
            self._newlines.append(pos)
            pos = text.find("\n", pos + 1)

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self._newlines, offset - 1) + 1


def _split_module_bodies(text: str) -> List[Tuple[str, str, str, int]]:
    """Yield ``(name, ports_text, body_text, body_offset)`` per module."""
    results: List[Tuple[str, str, str, int]] = []
    for match in _MODULE_RE.finditer(text):
        name = match.group("name").strip()
        ports = (match.group("ports") or "").strip()
        body_start = match.end()
        end = _ENDMODULE_RE.search(text, body_start)
        body_end = end.start() if end else len(text)
        body = text[body_start:body_end]
        results.append((name, ports, body, body_start))
    return results


def _parse_ports(body: str) -> List[Pin]:
    """Parse ``input``/``output``/``inout`` declarations into :class:`Pin`."""
    pins: List[Pin] = []
    for decl in _PORT_DECL_RE.finditer(body):
        direction = decl.group("dir")
        names = decl.group("names")
        for raw in names.split(","):
            name = raw.strip()
            if name:
                pins.append(Pin(name=name, direction=direction))
    return pins


def _looks_like_instance(type_token: str) -> bool:
    return type_token.lower() not in _VERILOG_KEYWORDS


def _parse_connections(conns: str) -> List[Pin]:
    """Parse the connection list of one instantiation."""
    pins: List[Pin] = []
    named = list(_NAMED_CONN_RE.finditer(conns))
    if named:
        for m in named:
            net = m.group("net").strip()
            pins.append(Pin(name=m.group("pin"), net=net or None))
        return pins
    # Positional fallback: split top-level commas.
    parts = [p.strip() for p in conns.split(",") if p.strip()]
    for idx, part in enumerate(parts):
        pins.append(Pin(name=f"${idx}", net=part))
    return pins


def classify_pin_direction(pin_name: str) -> str:
    """Heuristically classify a pin as input/output by common naming.

    This only affects driver/load inference for leaf cells where we have no
    module definition. Documented as a heuristic.

    Scan pins are named explicitly rather than left to the prefix rules: a
    scan output ``so``/``to`` starts with a letter the generic rules read as
    an input, and mis-directing a scan-out pin makes a scan cell look like it
    has no scan connection at all.
    """
    name = pin_name.lower()
    if name in _SCAN_OUT_PINS:
        return "output"
    if name in _SCAN_IN_PINS or name in _SHIFT_ENABLE_PINS:
        return "input"
    output_like = ("q", "qn", "y", "z", "o", "out", "co", "s", "sum")
    input_like = ("a", "b", "c", "d", "ci", "ck", "clk", "clock", "rn", "sn",
                  "se", "si", "ti", "te", "rst", "reset", "en", "in")
    if name in output_like or name.startswith(("y", "q", "z", "o", "out")):
        return "output"
    if name in input_like or name.startswith(("a", "b", "d", "in", "s")):
        return "input"
    return "unknown"


#: Backwards-compatible private alias.
_classify_pin_direction = classify_pin_direction


def _build_nets(module: Module) -> None:
    """Populate ``module.nets`` with driver/load relationships."""
    for pin in module.ports:
        net = module.nets.setdefault(pin.name, Net(name=pin.name, is_port=True))
        net.is_port = True
        net.port_direction = pin.direction

    for inst in module.instances.values():
        for pin in inst.pins:
            if not pin.net:
                continue
            net = module.nets.setdefault(pin.net, Net(name=pin.net))
            direction = pin.direction
            if direction == "unknown":
                direction = _classify_pin_direction(pin.name)
            if direction == "output":
                net.drivers.append((inst.name, pin.name))
            else:
                net.loads.append((inst.name, pin.name))


def parse_verilog(text: str) -> VerilogNetlist:
    """Parse Verilog *text* into a :class:`VerilogNetlist`.

    Args:
        text: Raw Verilog source (one or more modules).

    Returns:
        A populated :class:`VerilogNetlist`. Parsing never raises on malformed
        constructs; problems are recorded in ``netlist.warnings`` instead.
    """
    netlist = VerilogNetlist()
    clean = _strip_comments(text)
    lines = _LineIndex(clean)

    bodies = _split_module_bodies(clean)
    if not bodies:
        netlist.warnings.append("No 'module ... endmodule' blocks were found.")
        return netlist

    for name, _ports_text, body, body_offset in bodies:
        module = Module(name=name)
        module.ports = _parse_ports(body)
        module.line_number = lines.line_of(body_offset)

        # Blank out port/wire declarations before scanning for instances so
        # that declarations are not mistaken for instantiations. Blanking (not
        # deleting) keeps every offset aligned with the source file.
        scan_body = _PORT_DECL_RE.sub(_blank, body)
        scan_body = re.sub(
            r"\b(wire|reg|supply0|supply1|tri)\b[^;]*;", _blank, scan_body
        )
        scan_body = re.sub(r"\bassign\b[^;]*;", _blank, scan_body)

        for inst_match in _INSTANCE_RE.finditer(scan_body):
            type_token = inst_match.group("type")
            if not _looks_like_instance(type_token):
                continue
            inst_name = inst_match.group("name")
            conns = inst_match.group("conns")
            pins = _parse_connections(conns)
            instance = Instance(
                name=inst_name,
                cell_type=type_token,
                module=name,
                pins=pins,
                # Verbatim source, kept so a scan-status claim can quote the
                # instantiation instead of paraphrasing a table row.
                source_text=inst_match.group(0).strip(),
                line_number=lines.line_of(body_offset + inst_match.start()),
            )
            if inst_name in module.instances:
                netlist.warnings.append(
                    f"Duplicate instance '{inst_name}' in module '{name}'."
                )
            module.instances[inst_name] = instance

        _build_nets(module)
        if name in netlist.modules:
            netlist.warnings.append(f"Duplicate module definition '{name}'.")
        netlist.modules[name] = module

    netlist.infer_top()
    logger.info(
        "Parsed %d module(s); top inferred as '%s'.",
        len(netlist.modules),
        netlist.top_module,
    )
    return netlist


def parse_verilog_file(path: str) -> VerilogNetlist:
    """Read *path* (optionally gzip-compressed) and parse it."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        return parse_verilog(handle.read())


# ---------------------------------------------------------------------------
# Documented assumptions & limitations (also surfaced to the user)
# ---------------------------------------------------------------------------
ASSUMPTIONS = [
    "Only the structural Verilog subset is understood; behavioural RTL is "
    "ignored.",
    "Pin directions for leaf cells without a module definition are guessed "
    "from conventional pin names (Q/Y/Z=output, A/B/D/CK=input).",
    "Positional instance connections are supported but pin names become "
    "positional ($0, $1, ...).",
    "Parameter overrides, generate loops and macros are not elaborated.",
    "Bus/range expressions are kept as opaque net-name strings.",
]
