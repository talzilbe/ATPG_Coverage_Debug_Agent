"""Connectivity model built on top of a parsed netlist.

Wraps the structural netlist in a directed graph so that fan-in/fan-out and
bounded cone tracing become cheap. ``networkx`` is used when available; a small
pure-Python fallback keeps the tool functional without the dependency.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..models import Instance
from ..parser.verilog_parser import VerilogNetlist, classify_pin_direction

logger = logging.getLogger(__name__)

try:  # optional dependency
    import networkx as nx  # type: ignore

    _HAVE_NX = True
except Exception:  # pragma: no cover - exercised only without networkx
    _HAVE_NX = False


#: Hierarchy levels a driver search may cross. Feedthrough ports routinely
#: stack 3-5 deep in a synthesised partition, so a bound of 3 is far too tight.
DEFAULT_MAX_HOPS = 24

#: Library naming for constant drivers.
#:
#: :func:`is_tie_cell` recognises a tie structurally -- a cell with pins but no
#: inputs -- which works for any library. Naming is only needed for the one
#: thing structure cannot answer: a tie-high and a tie-low cell are both
#: output-only and therefore structurally identical, so :func:`tie_value` can
#: only tell 0 from 1 by the cell name.
#:
#: The defaults below cover the abbreviations libraries actually use
#: (``tiehi``/``tielo``, ``tihi``/``tilo``, ``tieh``/``tiel``, ``thi``/``tlo``,
#: ``logic1``/``logic0``, ``const1``/``const0``), so no configuration is
#: required. ``thi``/``tlo`` keep a trailing word boundary because the bare
#: forms occur inside ordinary words ("something" contains "thi").
#: ``ATPG_TIE_HIGH_PATTERNS`` / ``ATPG_TIE_LOW_PATTERNS`` are an optional
#: escape hatch for a library that names them some other way; each holds extra
#: regex alternatives separated by ``|``.
_TIE_HIGH_DEFAULT = r"tiehi|tihi|tieh|thi\b|tie1|logic1|const1"
_TIE_LOW_DEFAULT = r"tielo|tilo|tiel|tlo\b|tie0|logic0|const0"


def _pattern_body(env_var: str, default: str) -> str:
    extra = os.environ.get(env_var, "").strip()
    return f"{default}|{extra}" if extra else default


_TIE_HIGH_BODY = _pattern_body("ATPG_TIE_HIGH_PATTERNS", _TIE_HIGH_DEFAULT)
_TIE_LOW_BODY = _pattern_body("ATPG_TIE_LOW_PATTERNS", _TIE_LOW_DEFAULT)

_TIE_CELL_TYPE = re.compile(
    f"({_TIE_HIGH_BODY}|{_TIE_LOW_BODY}|_tie)", re.I)
_TIE_HIGH_TYPE = re.compile(f"({_TIE_HIGH_BODY})", re.I)
_TIE_LOW_TYPE = re.compile(f"({_TIE_LOW_BODY})", re.I)


def _pin_direction(pin) -> str:
    if pin.direction and pin.direction != "unknown":
        return pin.direction
    return classify_pin_direction(pin.name)


def is_tie_cell(inst: Instance, netlist: Optional[VerilogNetlist] = None) -> bool:
    """True when *inst* is a constant (tie) driver.

    Evidence is weighted, not simply OR-ed:

    * **Structural (decisive)** -- the instance has pins, and none of them is
      an input. A cell that consumes nothing and produces something is a
      constant source; this catches tie cells from any library, named
      anything. Conversely a cell with a known input pin consumes a value and
      is *not* a constant source, whatever it happens to be called.
    * **Naming (fallback only)** -- consulted when the structure is silent:
      the instance records no pins, or none of its pins could be classified.
      A cell type matching a tie convention is then the only evidence there is.

    Naming can therefore rescue a tie the parser could not resolve, but it can
    never override structure that positively shows an input. That ordering
    matters: a wrapper called something like ``and2_tie_hold`` has inputs and
    must not be reported as a constant driver on the strength of its name.

    A hierarchical instance (its cell type is a module we parsed) is never a
    tie cell, however its ports are named.
    """
    if netlist is not None and inst.cell_type in netlist.modules:
        return False
    directions = [_pin_direction(pin) for pin in inst.pins]
    if directions and all(d == "output" for d in directions):
        return True
    if any(d == "input" for d in directions):
        return False
    return bool(_TIE_CELL_TYPE.search(inst.cell_type or ""))


def tie_value(cell_type: str) -> Optional[str]:
    """Return ``'1'`` / ``'0'`` for a tie cell type, else ``None``."""
    if _TIE_HIGH_TYPE.search(cell_type or ""):
        return "1"
    if _TIE_LOW_TYPE.search(cell_type or ""):
        return "0"
    return None


@dataclass
class DriverResolution:
    """The gate finally reached when resolving a net across hierarchy.

    Attributes:
        instance: The terminal driving instance (a leaf cell).
        module: Module the driver lives in.
        net: The net it drives, in that module.
        pin: The driving pin.
        trace: Human-readable hops taken, in order, so the walk is auditable.
        is_tie: True when the terminal driver is a constant source.
        tie_value: ``'0'`` / ``'1'`` when inferable from the cell type.
        load_count: Loads on the terminal net (its fan-out).
    """

    instance: Instance
    module: str
    net: str
    pin: str
    trace: List[str] = field(default_factory=list)
    is_tie: bool = False
    tie_value: Optional[str] = None
    load_count: int = 0

    @property
    def levels(self) -> int:
        """Hierarchy hops crossed to reach the driver."""
        return len(self.trace)

    def describe(self) -> str:
        """One-line summary quoting the driver and the hops taken."""
        kind = "tie cell" if self.is_tie else "gate"
        value = f" (constant {self.tie_value})" if self.tie_value else ""
        return (f"{kind} '{self.instance.name}' ({self.instance.cell_type})"
                f"{value} in module '{self.module}' via {self.levels} hop(s)")


class ConnectivityModel:
    """Driver/load connectivity across all parsed modules.

    Nodes are instance names (qualified by module to avoid collisions) and
    nets. Edges flow ``driver_instance -> net -> load_instance``.
    """

    def __init__(self, netlist: VerilogNetlist) -> None:
        self.netlist = netlist
        #: instance key -> Instance
        self.instances: Dict[str, Instance] = {}
        #: net key -> list of driver instance keys
        self._net_drivers: Dict[str, List[str]] = {}
        #: net key -> list of load instance keys
        self._net_loads: Dict[str, List[str]] = {}
        #: instance key -> nets it drives
        self._inst_out_nets: Dict[str, List[str]] = {}
        #: instance key -> nets it loads
        self._inst_in_nets: Dict[str, List[str]] = {}
        #: module name -> [(parent_module, instance_name)] instantiating it
        self._module_parents: Dict[str, List[Tuple[str, str]]] = {}
        #: (module, net, max_hops) -> resolved driver. The netlist is immutable
        #: for the life of this model, so a resolution can never change. One
        #: tie cell commonly sits upstream of tens of thousands of faults, and
        #: without this each of them re-walks the same hierarchy.
        self._driver_cache: Dict[Tuple[str, str, int],
                                 Optional["DriverResolution"]] = {}
        self._graph = nx.DiGraph() if _HAVE_NX else None
        self._build()

    # -- construction -----------------------------------------------------
    @staticmethod
    def inst_key(module: str, inst_name: str) -> str:
        return f"{module}::{inst_name}"

    @staticmethod
    def net_key(module: str, net_name: str) -> str:
        return f"{module}::net::{net_name}"

    def _build(self) -> None:
        for mod_name, module in self.netlist.modules.items():
            for inst in module.instances.values():
                ikey = self.inst_key(mod_name, inst.name)
                self.instances[ikey] = inst
                self._inst_in_nets.setdefault(ikey, [])
                self._inst_out_nets.setdefault(ikey, [])
                if inst.cell_type in self.netlist.modules:
                    self._module_parents.setdefault(
                        inst.cell_type, []).append((mod_name, inst.name))
                if self._graph is not None:
                    self._graph.add_node(ikey, kind="instance",
                                         cell_type=inst.cell_type)

            for net in module.nets.values():
                nkey = self.net_key(mod_name, net.name)
                self._net_drivers.setdefault(nkey, [])
                self._net_loads.setdefault(nkey, [])
                for inst_name, _pin in net.drivers:
                    dkey = self.inst_key(mod_name, inst_name)
                    self._net_drivers[nkey].append(dkey)
                    self._inst_out_nets.setdefault(dkey, []).append(net.name)
                for inst_name, _pin in net.loads:
                    lkey = self.inst_key(mod_name, inst_name)
                    self._net_loads[nkey].append(lkey)
                    self._inst_in_nets.setdefault(lkey, []).append(net.name)

                if self._graph is not None:
                    for dkey in self._net_drivers[nkey]:
                        for lkey in self._net_loads[nkey]:
                            self._graph.add_edge(dkey, lkey, net=net.name)
        logger.info("Connectivity model built: %d instances.",
                    len(self.instances))

    # -- queries ----------------------------------------------------------
    def find_instance(self, module: str, inst_name: str) -> Optional[Instance]:
        return self.instances.get(self.inst_key(module, inst_name))

    def immediate_fan_in(self, module: str, inst_name: str) -> List[str]:
        """Return instance names that drive any input net of the instance."""
        ikey = self.inst_key(module, inst_name)
        result: Set[str] = set()
        for net_name in self._inst_in_nets.get(ikey, []):
            nkey = self.net_key(module, net_name)
            for dkey in self._net_drivers.get(nkey, []):
                if dkey != ikey:
                    result.add(self.instances[dkey].name if dkey in
                               self.instances else dkey)
        return sorted(result)

    def immediate_fan_out(self, module: str, inst_name: str) -> List[str]:
        """Return instance names loaded by any output net of the instance."""
        ikey = self.inst_key(module, inst_name)
        result: Set[str] = set()
        for net_name in self._inst_out_nets.get(ikey, []):
            nkey = self.net_key(module, net_name)
            for lkey in self._net_loads.get(nkey, []):
                if lkey != ikey:
                    result.add(self.instances[lkey].name if lkey in
                               self.instances else lkey)
        return sorted(result)

    def net_driver_instances(self, module: str, net_name: str) -> List[str]:
        nkey = self.net_key(module, net_name)
        return [self.instances[k].name for k in self._net_drivers.get(nkey, [])
                if k in self.instances]

    def net_load_instances(self, module: str, net_name: str) -> List[str]:
        nkey = self.net_key(module, net_name)
        return [self.instances[k].name for k in self._net_loads.get(nkey, [])
                if k in self.instances]

    def trace_cone(self, module: str, inst_name: str, *, direction: str,
                   max_depth: int = 3) -> List[Tuple[str, int]]:
        """Bounded cone trace upstream (``in``) or downstream (``out``).

        Returns ``(instance_name, depth)`` pairs, depth-first bounded by
        *max_depth*. Used for evidence gathering, hence the small default.
        """
        start = self.inst_key(module, inst_name)
        if start not in self.instances:
            return []
        visited: Set[str] = {start}
        frontier = [(start, 0)]
        out: List[Tuple[str, int]] = []
        step = (self._upstream_keys if direction == "in"
                else self._downstream_keys)
        while frontier:
            key, depth = frontier.pop()
            if depth >= max_depth:
                continue
            for nxt in step(module, key):
                if nxt in visited:
                    continue
                visited.add(nxt)
                name = self.instances[nxt].name if nxt in self.instances else nxt
                out.append((name, depth + 1))
                frontier.append((nxt, depth + 1))
        return out

    def _upstream_keys(self, module: str, ikey: str) -> List[str]:
        keys: List[str] = []
        for net_name in self._inst_in_nets.get(ikey, []):
            nkey = self.net_key(module, net_name)
            keys.extend(self._net_drivers.get(nkey, []))
        return keys

    def _downstream_keys(self, module: str, ikey: str) -> List[str]:
        keys: List[str] = []
        for net_name in self._inst_out_nets.get(ikey, []):
            nkey = self.net_key(module, net_name)
            keys.extend(self._net_loads.get(nkey, []))
        return keys

    def downstream(self, inst_key: str) -> List[str]:
        """Public successor query: instance keys driven by *inst_key*.

        The module is recovered from the key (``module::inst_name``) so callers
        can navigate the graph without tracking the module separately.
        """
        module = inst_key.split("::", 1)[0]
        out: List[str] = []
        for lkey in self._downstream_keys(module, inst_key):
            if lkey != inst_key:
                out.append(lkey)
        return out

    def upstream(self, inst_key: str) -> List[str]:
        """Public predecessor query: instance keys driving *inst_key*."""
        module = inst_key.split("::", 1)[0]
        out: List[str] = []
        for dkey in self._upstream_keys(module, inst_key):
            if dkey != inst_key:
                out.append(dkey)
        return out

    # -- hierarchical driver resolution -----------------------------------
    def parents_of(self, module_name: str) -> List[Tuple[str, str]]:
        """Return ``(parent_module, instance_name)`` instantiating *module_name*."""
        return list(self._module_parents.get(module_name, ()))

    def resolve_driver(self, module: str, net_name: Optional[str], *,
                       max_hops: int = DEFAULT_MAX_HOPS
                       ) -> Optional["DriverResolution"]:
        """Follow *net_name* to the gate that actually drives it.

        Ports in a synthesised hierarchy are overwhelmingly feedthroughs: a
        pin can be bound to a port that is bound to a port that is bound to a
        port before any gate appears. Stopping at the first port and calling
        the net "undriven" is what makes a hard constant look like missing
        connectivity, so this walks the hierarchy in both directions until it
        reaches a leaf cell:

        * a net with no in-module driver that *is* an input port -> go **up**
          into the parent module and continue from the net bound to that port;
        * a net driven by an instance of a module we parsed -> go **down**
          into that module and continue from the driving output port;
        * a net driven by a leaf cell -> that cell is the terminal driver.

        Args:
            module: Module containing *net_name*.
            net_name: The net to resolve.
            max_hops: Safety bound on hierarchy traversal.

        Returns:
            A :class:`DriverResolution`, or ``None`` when the driver cannot be
            reached (unknown module, ambiguous parent, or hop budget spent).
            ``None`` means *unknown*, never *no driver*.
        """
        if not net_name:
            return None
        cache_key = (module, net_name, int(max_hops))
        if cache_key in self._driver_cache:
            return self._driver_cache[cache_key]
        resolved = self._resolve_driver_uncached(module, net_name, max_hops)
        self._driver_cache[cache_key] = resolved
        return resolved

    def _resolve_driver_uncached(self, module: str, net_name: str,
                                 max_hops: int
                                 ) -> Optional["DriverResolution"]:
        """The hierarchy walk behind :meth:`resolve_driver`, without caching."""
        cur_mod, cur_net = module, net_name
        trace: List[str] = []
        seen: Set[Tuple[str, str]] = set()

        for _ in range(max_hops):
            if (cur_mod, cur_net) in seen:
                return None
            seen.add((cur_mod, cur_net))
            mod_obj = self.netlist.modules.get(cur_mod)
            if mod_obj is None:
                return None
            net_obj = mod_obj.nets.get(cur_net)
            drivers = list(net_obj.drivers) if net_obj is not None else []

            if drivers:
                inst_name, pin_name = drivers[0]
                inst = mod_obj.instances.get(inst_name)
                if inst is None:
                    return None
                sub = self.netlist.modules.get(inst.cell_type)
                if sub is None:
                    trace.append(f"{cur_mod}/{inst_name}.{pin_name} "
                                 f"drives net '{cur_net}'")
                    return DriverResolution(
                        instance=inst, module=cur_mod, net=cur_net,
                        pin=pin_name, trace=trace,
                        is_tie=is_tie_cell(inst, self.netlist),
                        tie_value=tie_value(inst.cell_type),
                        load_count=len(net_obj.loads) if net_obj else 0,
                    )
                # Hierarchical instance: descend through the driving port.
                trace.append(f"{cur_mod}/{inst_name}.{pin_name} -> descend "
                             f"into module '{inst.cell_type}'")
                cur_mod, cur_net = inst.cell_type, pin_name
                continue

            # No in-module driver. If this is an input port, climb one level.
            if not self._is_input_port(mod_obj, cur_net):
                return None
            parents = self._module_parents.get(cur_mod, [])
            if len(parents) != 1:
                # Zero parents = top level; many = the binding is ambiguous
                # and guessing one would fabricate connectivity.
                return None
            parent_mod, parent_inst_name = parents[0]
            parent_inst = self.netlist.modules[parent_mod].instances.get(
                parent_inst_name)
            if parent_inst is None:
                return None
            bound = parent_inst.pin_net(cur_net)
            if not bound:
                return None
            trace.append(f"port '{cur_net}' of module '{cur_mod}' <- "
                         f"{parent_mod}/{parent_inst_name} net '{bound}'")
            cur_mod, cur_net = parent_mod, bound

        return None

    @staticmethod
    def _is_input_port(mod_obj, net_name: str) -> bool:
        for port in mod_obj.ports:
            if port.name == net_name:
                return port.direction in ("input", "inout")
        net = mod_obj.nets.get(net_name)
        return bool(net is not None and net.is_port
                    and net.port_direction in ("input", "inout", None))

    def net_fanout_count(self, module: str, net_name: str) -> int:
        """Number of loads on *net_name* inside *module*."""
        mod_obj = self.netlist.modules.get(module)
        if mod_obj is None:
            return 0
        net = mod_obj.nets.get(net_name)
        return len(net.loads) if net is not None else 0

