"""Decide whether a cell is a scan cell -- from netlist pin evidence only.

This module exists because the agent once answered "non-scan" for a flop that
had ``.si``, ``.ssb`` and ``.so`` in its instantiation. It reached that answer
from a fault-table row showing ``fanin=0, fanout=0, confidence=unresolved``,
read the zeros as "not connected to a scan chain", and built an entire
narrative on top. The zeros meant the extractor had failed to map the object.

The rule enforced here is therefore absolute:

* A cell is **SCAN** only when its instantiation was literally read and shows
  both a dedicated scan-data input and a shift-enable pin.
* A cell is **NON-SCAN** only when its instantiation was literally read and
  shows neither.
* Anything else -- above all, a fault row that never mapped -- is
  **UNRESOLVED**, and the answer is :data:`UNRESOLVED_ANSWER` verbatim.

Naming conventions, fan-in/fan-out counts, mapping confidence and the "scan
boundary involved" column are never sufficient on their own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import Instance, MappingConfidence
from ..parser.verilog_parser import VerilogNetlist, scan_pin_role
from .connectivity import ConnectivityModel, DriverResolution

logger = logging.getLogger(__name__)

#: The one permitted answer when no instantiation could be read. Emitted
#: verbatim -- callers and tests compare against this exact string.
UNRESOLVED_ANSWER = (
    "Unresolved - scan status cannot be determined without netlist pin "
    "evidence."
)

SCAN = "scan"
NON_SCAN = "non_scan"
UNRESOLVED = "unresolved"

#: Global shift-enable nets a shift-enable pin is expected to trace back to.
_GLOBAL_SE_TOKENS = ("test_se", "scan_en", "scan_enable", "shift_en",
                     "test_mode_se", "se_", "_se")


@dataclass
class ScanStatus:
    """Verdict plus the literal evidence it was drawn from.

    Attributes:
        verdict: ``scan`` / ``non_scan`` / ``unresolved``.
        target: The object asked about, verbatim.
        instance / cell_type / module / line_number: The instantiation read.
        instantiation: Verbatim instantiation text, all continuation lines.
        scan_in / scan_out / shift_enable: ``(pin, net)`` when present.
        chain_connected: ``True`` when scan-in is driven by real logic,
            ``False`` when it is tied off (scan-capable but not in a chain),
            ``None`` when it could not be resolved.
        corroboration: Results of the three independent cross-checks.
        evidence: Ordered, human-readable findings.
        blockers: Why the verdict is unresolved, when it is.
    """

    verdict: str
    target: str
    instance: Optional[str] = None
    cell_type: Optional[str] = None
    module: Optional[str] = None
    line_number: Optional[int] = None
    instantiation: str = ""
    scan_in: Optional[tuple] = None
    scan_out: Optional[tuple] = None
    shift_enable: Optional[tuple] = None
    chain_connected: Optional[bool] = None
    corroboration: Dict[str, str] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    @property
    def is_scan(self) -> bool:
        return self.verdict == SCAN

    def answer(self) -> str:
        """The sentence the agent is required to emit for this verdict."""
        if self.verdict == UNRESOLVED:
            return UNRESOLVED_ANSWER
        if self.verdict == SCAN:
            pins = ", ".join(
                f".{p}" for p, _ in
                (x for x in (self.scan_in, self.shift_enable, self.scan_out)
                 if x)
            )
            tail = ("" if self.chain_connected is not False else
                    " (scan-capable but NOT chain-connected: scan-in is tied)")
            return (f"SCAN - instantiation at line {self.line_number} shows "
                    f"{pins}{tail}.")
        return (f"NON-SCAN - instantiation at line {self.line_number} has no "
                f"scan-data input and no shift-enable pin.")

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for tool responses and serialisation."""
        return {
            "verdict": self.verdict,
            "answer": self.answer(),
            "target": self.target,
            "instance": self.instance,
            "cell_type": self.cell_type,
            "module": self.module,
            "line_number": self.line_number,
            "instantiation": self.instantiation,
            "scan_in": list(self.scan_in) if self.scan_in else None,
            "scan_out": list(self.scan_out) if self.scan_out else None,
            "shift_enable": (list(self.shift_enable)
                             if self.shift_enable else None),
            "chain_connected": self.chain_connected,
            "corroboration": dict(self.corroboration),
            "evidence": list(self.evidence),
            "blockers": list(self.blockers),
        }


def unresolved(target: str, *blockers: str) -> ScanStatus:
    """Build the unresolved verdict with the reasons it could not be decided."""
    return ScanStatus(
        verdict=UNRESOLVED,
        target=target,
        blockers=list(blockers),
        evidence=[UNRESOLVED_ANSWER],
    )


def classify_instance(inst: Instance, module: str,
                      conn: Optional[ConnectivityModel] = None) -> ScanStatus:
    """Classify a single instantiation that has already been located.

    Args:
        inst: The instance, carrying its verbatim ``source_text``.
        module: Module the instance is defined in.
        conn: Optional connectivity model, used only for corroboration.

    Returns:
        A :class:`ScanStatus`. Never guesses: an instance with no parsed pins
        yields ``unresolved`` rather than ``non_scan``.
    """
    status = ScanStatus(
        verdict=UNRESOLVED,
        target=inst.name,
        instance=inst.name,
        cell_type=inst.cell_type,
        module=module,
        line_number=inst.line_number,
        instantiation=inst.source_text,
    )

    if not inst.pins:
        status.blockers.append(
            f"Instance '{inst.name}' has no parsed pin list, so no pin "
            f"evidence exists."
        )
        status.evidence.append(UNRESOLVED_ANSWER)
        return status

    for pin in inst.pins:
        role = scan_pin_role(pin.name)
        if role == "scan_in" and status.scan_in is None:
            status.scan_in = (pin.name, pin.net)
        elif role == "scan_out" and status.scan_out is None:
            status.scan_out = (pin.name, pin.net)
        elif role == "shift_enable" and status.shift_enable is None:
            status.shift_enable = (pin.name, pin.net)

    quoted = inst.source_text or _render_instantiation(inst)
    status.evidence.append(
        f"Read instantiation at line {inst.line_number} in module "
        f"'{module}': {quoted}"
    )

    # A cell is SCAN when it has a dedicated scan-data input AND a
    # shift-enable pin. Scan-out alone is not enough (some cells expose a
    # buffered Q under an output-ish name) and neither is naming.
    if status.scan_in and status.shift_enable:
        status.verdict = SCAN
        status.evidence.append(
            f"Scan-data input .{status.scan_in[0]}({status.scan_in[1]}) and "
            f"shift-enable .{status.shift_enable[0]}"
            f"({status.shift_enable[1]}) are both present."
        )
        if status.scan_out:
            status.evidence.append(
                f"Scan output .{status.scan_out[0]}({status.scan_out[1]}) is "
                f"present."
            )
        _corroborate(status, inst, module, conn)
        return status

    if not status.scan_in and not status.shift_enable:
        status.verdict = NON_SCAN
        status.evidence.append(
            "No scan-data input and no shift-enable pin in the pin list; "
            "the cell is non-scan."
        )
        return status

    # Exactly one of the two -- an unusual cell or an unknown pin convention.
    present = "scan-data input" if status.scan_in else "shift-enable"
    missing = "shift-enable" if status.scan_in else "scan-data input"
    status.blockers.append(
        f"Pin list has a {present} but no recognised {missing}; the pin "
        f"naming convention of '{inst.cell_type}' is not covered. Confirm "
        f"against the library model before claiming a scan status."
    )
    status.evidence.append(UNRESOLVED_ANSWER)
    return status


def determine_scan_status(target: str, mapper: Any,
                          conn: Optional[ConnectivityModel] = None,
                          netlist: Optional[VerilogNetlist] = None
                          ) -> ScanStatus:
    """Answer "is this a scan cell?" for a fault object or instance path.

    Args:
        target: Fault object or hierarchical instance path.
        mapper: A :class:`~..analysis.mapper.FaultMapper`, or ``None``.
        conn: Connectivity model used for corroboration.
        netlist: Parsed netlist; required to read an instantiation.

    Returns:
        A :class:`ScanStatus`. When no netlist is available, or the object
        does not map onto one, the verdict is ``unresolved`` and
        :meth:`ScanStatus.answer` is :data:`UNRESOLVED_ANSWER` -- the fault
        table alone can never decide this.
    """
    if netlist is None and conn is not None:
        netlist = conn.netlist
    if mapper is None or netlist is None or not netlist.modules:
        return unresolved(
            target,
            "No parsed netlist is loaded. Scan status requires reading the "
            "instantiation; fault-table fields (fan-in, fan-out, mapped "
            "instance, confidence, scan-boundary column) carry no pin "
            "evidence.",
        )

    mapping = mapper.map_object(target)
    if mapping.confidence is MappingConfidence.UNRESOLVED:
        detail = (f" {len(mapping.candidates)} candidate instance(s) matched "
                  f"the leaf name." if mapping.candidates else "")
        return unresolved(
            target,
            f"'{target}' did not map onto a netlist instance, so its "
            f"instantiation was never read.{detail} Any fan-in/fan-out of 0 "
            f"reported for it is a mapping failure, not measured "
            f"connectivity.",
        )

    module = mapping.module_name
    inst = None
    if module and module in netlist.modules:
        inst = netlist.modules[module].instances.get(mapping.instance_name)
    if inst is None:
        return unresolved(
            target,
            f"Mapped to instance '{mapping.instance_name}' but its "
            f"definition could not be re-read from the netlist.",
        )

    status = classify_instance(inst, module, conn)
    status.target = target
    return status


# ---------------------------------------------------------------------------
# Corroboration (Step 5a(d)) -- reported even when the pin list is conclusive
# ---------------------------------------------------------------------------
def _corroborate(status: ScanStatus, inst: Instance, module: str,
                 conn: Optional[ConnectivityModel]) -> None:
    """Run the three independent cross-checks and record their outcome."""
    if conn is None:
        status.corroboration["note"] = (
            "not run - no connectivity model available")
        return

    # 1. Shift-enable should trace back to a global test_se / scan_en.
    se_net = status.shift_enable[1] if status.shift_enable else None
    status.corroboration["shift_enable"] = _describe_shift_enable(
        conn, module, se_net)

    # 2. Scan-out should reach a module output port.
    so_net = status.scan_out[1] if status.scan_out else None
    status.corroboration["scan_out"] = _describe_scan_out(
        conn, module, so_net)

    # 3. Scan-in should be driven by real logic, not a tie cell.
    si_net = status.scan_in[1] if status.scan_in else None
    driver = conn.resolve_driver(module, si_net) if si_net else None
    if driver is None:
        status.corroboration["scan_in"] = (
            f"driver of scan-in net '{si_net}' could not be resolved; "
            f"chain connection unconfirmed")
        status.chain_connected = None
    elif driver.is_tie:
        status.corroboration["scan_in"] = (
            f"scan-in net '{si_net}' is driven by {driver.describe()} - the "
            f"cell is scan-CAPABLE but NOT chain-connected")
        status.chain_connected = False
    else:
        status.corroboration["scan_in"] = (
            f"scan-in net '{si_net}' is driven by {driver.describe()}")
        status.chain_connected = True

    for key in ("shift_enable", "scan_out", "scan_in"):
        if key in status.corroboration:
            status.evidence.append(
                f"Corroboration ({key}): {status.corroboration[key]}")


def _describe_shift_enable(conn: ConnectivityModel, module: str,
                           net: Optional[str]) -> str:
    if not net:
        return "no shift-enable net recorded"
    driver: Optional[DriverResolution] = conn.resolve_driver(module, net)
    if driver is None:
        return (f"net '{net}' could not be traced to a driver; global "
                f"shift-enable unconfirmed")
    lowered = f"{driver.net}".lower()
    looks_global = any(tok in lowered for tok in _GLOBAL_SE_TOKENS)
    verdict = ("consistent with a global shift-enable" if looks_global
               else "global shift-enable NOT confirmed by net naming")
    return (f"net '{net}' traces through {driver.levels} hop(s) to "
            f"{driver.describe()} on net '{driver.net}' - {verdict}")


def _describe_scan_out(conn: ConnectivityModel, module: str,
                       net: Optional[str]) -> str:
    if not net:
        return "no scan-out net recorded"
    mod_obj = conn.netlist.modules.get(module)
    if mod_obj is None:
        return f"module '{module}' not found"
    for port in mod_obj.ports:
        if port.name == net:
            return (f"net '{net}' is an {port.direction} port of module "
                    f"'{module}' - scan-out reaches a module boundary")
    net_obj = mod_obj.nets.get(net)
    if net_obj is not None and net_obj.is_port:
        return (f"net '{net}' is a port of module '{module}' "
                f"(direction {net_obj.port_direction or 'unknown'})")
    loads = conn.net_load_instances(module, net)
    if loads:
        return (f"net '{net}' is not a port of '{module}'; it loads "
                f"{len(loads)} instance(s), e.g. {loads[0]}")
    return f"net '{net}' has no recorded loads and is not a module port"


def _render_instantiation(inst: Instance) -> str:
    """Reconstruct an instantiation when no verbatim source was retained."""
    conns = " , ".join(f".{p.name} ( {p.net or ''} )" for p in inst.pins)
    return f"{inst.cell_type} {inst.name} ( {conns} ) ;"
