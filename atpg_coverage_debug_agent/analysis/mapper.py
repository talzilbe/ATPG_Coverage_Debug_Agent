"""Correlate fault-list objects with netlist objects.

Fault objects from a flattened ATPG run rarely match the hierarchical netlist
exactly. This module implements a tiered matching strategy and is explicit
about ambiguity: it returns candidate matches plus a confidence level rather
than silently guessing.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from ..models import Instance, MappingConfidence, MappingResult
from ..parser.fault_parser import normalize_object
from .connectivity import ConnectivityModel

logger = logging.getLogger(__name__)


class FaultMapper:
    """Maps normalised fault objects to netlist instances/nets.

    Matching tiers (highest to lowest confidence):

    1. **exact** -- normalised fault object equals a fully-qualified pin path.
    2. **normalized** -- last instance segment matches a unique instance name.
    3. **flattened heuristic** -- the trailing path segments match an instance
       whose hierarchical leaf name is a suffix of the fault object.
    4. **unresolved** -- nothing matched or the match was ambiguous.
    """

    def __init__(self, connectivity: ConnectivityModel) -> None:
        self.conn = connectivity
        # Index instance leaf name -> list of (module, instance).
        self._by_name: Dict[str, List[Tuple[str, Instance]]] = {}
        # Index normalised "module/instance" -> (module, instance).
        self._by_path: Dict[str, Tuple[str, Instance]] = {}
        self._build_index()

    def _build_index(self) -> None:
        for mod_name, module in self.conn.netlist.modules.items():
            for inst in module.instances.values():
                self._by_name.setdefault(inst.name, []).append((mod_name, inst))
                path = normalize_object(f"{mod_name}/{inst.name}")
                self._by_path[path] = (mod_name, inst)

    @staticmethod
    def _split_object(normalized: str) -> Tuple[str, Optional[str]]:
        """Return ``(instance_path, pin)`` guessing the trailing pin segment."""
        parts = normalized.split("/")
        if len(parts) >= 2:
            # The last segment is often the pin (Y/Q/A...) on a leaf cell.
            return "/".join(parts[:-1]), parts[-1]
        return normalized, None

    def map_object(self, fault_object: str) -> MappingResult:
        """Return the best :class:`MappingResult` for *fault_object*."""
        normalized = normalize_object(fault_object)
        inst_path, pin = self._split_object(normalized)
        leaf = inst_path.split("/")[-1] if inst_path else normalized
        evidence: List[str] = []

        # Tier 1: exact path match.
        if normalized in self._by_path:
            mod, inst = self._by_path[normalized]
            evidence.append(f"Exact path match in module '{mod}'.")
            return self._result(fault_object, normalized, inst, pin,
                                MappingConfidence.HIGH, evidence)

        if inst_path in self._by_path:
            mod, inst = self._by_path[inst_path]
            evidence.append(
                f"Exact instance-path match in module '{mod}' "
                f"(pin '{pin}')."
            )
            return self._result(fault_object, normalized, inst, pin,
                                MappingConfidence.HIGH, evidence)

        # Tier 2: unique leaf-name match.
        by_name = self._by_name.get(leaf, [])
        if len(by_name) == 1:
            mod, inst = by_name[0]
            evidence.append(
                f"Unique instance name '{leaf}' found in module '{mod}'."
            )
            return self._result(fault_object, normalized, inst, pin,
                                MappingConfidence.MEDIUM, evidence)

        # Tier 3: flattened-hierarchy suffix heuristic.
        suffix_matches = self._suffix_match(normalized)
        if len(suffix_matches) == 1:
            mod, inst = suffix_matches[0]
            evidence.append(
                "Flattened-hierarchy suffix heuristic matched a single "
                f"instance '{inst.name}' in module '{mod}'."
            )
            return self._result(fault_object, normalized, inst, pin,
                                MappingConfidence.LOW, evidence)

        # Ambiguous or no match.
        candidates = [f"{m}/{i.name}" for m, i in (by_name or suffix_matches)]
        if candidates:
            evidence.append(
                f"Ambiguous match: {len(candidates)} candidate instance(s)."
            )
        else:
            evidence.append("No structural match for this fault object.")
        return MappingResult(
            fault_object=fault_object,
            normalized_object=normalized,
            confidence=MappingConfidence.UNRESOLVED,
            candidates=candidates,
            evidence=evidence,
        )

    def _suffix_match(self, normalized: str) -> List[Tuple[str, Instance]]:
        """Find instances whose name is the last path segment of *normalized*.

        Heuristic for flat fault names: ``core_u_alu_U12`` may correspond to an
        instance ``U12`` reached through hierarchy. We match on the final
        underscore- or slash-delimited token.
        """
        tokens = normalized.replace("/", "_").split("_")
        results: List[Tuple[str, Instance]] = []
        for size in range(1, min(4, len(tokens)) + 1):
            candidate = "_".join(tokens[-size:])
            if candidate in self._by_name:
                results.extend(self._by_name[candidate])
        # De-duplicate while preserving order.
        seen = set()
        unique: List[Tuple[str, Instance]] = []
        for mod, inst in results:
            key = (mod, inst.name)
            if key not in seen:
                seen.add(key)
                unique.append((mod, inst))
        return unique

    def _result(self, fault_object: str, normalized: str, inst: Instance,
                pin: Optional[str], confidence: MappingConfidence,
                evidence: List[str]) -> MappingResult:
        matched_net = inst.pin_net(pin) if pin else None
        return MappingResult(
            fault_object=fault_object,
            normalized_object=normalized,
            confidence=confidence,
            instance_name=inst.name,
            cell_type=inst.cell_type,
            matched_net=matched_net,
            evidence=evidence,
        )
