"""Connectivity model built on top of a parsed netlist.

Wraps the structural netlist in a directed graph so that fan-in/fan-out and
bounded cone tracing become cheap. ``networkx`` is used when available; a small
pure-Python fallback keeps the tool functional without the dependency.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from ..models import Instance
from ..parser.verilog_parser import VerilogNetlist

logger = logging.getLogger(__name__)

try:  # optional dependency
    import networkx as nx  # type: ignore

    _HAVE_NX = True
except Exception:  # pragma: no cover - exercised only without networkx
    _HAVE_NX = False


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

