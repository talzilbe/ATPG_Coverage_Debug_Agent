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

    #: Ancestor levels compared when disambiguating a repeated leaf name.
    MAX_ANCESTOR_DEPTH = 6

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

        # Tiers 2 / 2b run over two readings of the path, most literal first:
        # the whole path may name an instance, or its last segment may be a
        # pin. Trying the pin reading first used to resolve a pin-less path to
        # its PARENT instance, which is how analysis ended up describing the
        # wrong cell entirely.
        interpretations: List[Tuple[str, Optional[str]]] = []
        if inst_path != normalized:
            interpretations.append((normalized, None))
        interpretations.append((inst_path, pin))

        for path, pin_name in interpretations:
            leaf_name = path.split("/")[-1] if path else path
            by_name = self._by_name.get(leaf_name, [])

            # Tier 2: unique leaf-name match.
            if len(by_name) == 1:
                mod, inst = by_name[0]
                evidence.append(
                    f"Unique instance name '{leaf_name}' found in module "
                    f"'{mod}'."
                )
                return self._result(fault_object, normalized, inst, pin_name,
                                    MappingConfidence.MEDIUM, evidence)

            # Tier 2b: duplicate leaf name disambiguated through the hierarchy.
            #
            # Replicated modules make leaf names repeat hundreds of times, and
            # "ambiguous" was previously the end of the road. It need not be:
            # the fault path names the parent instance, the parent instance
            # names its module type, and the leaf is defined inside exactly one
            # of those module bodies.
            if len(by_name) > 1:
                resolved = self._resolve_by_hierarchy(path)
                if len(resolved) == 1:
                    mod, inst = resolved[0]
                    parent = path.split("/")[-2]
                    evidence.append(
                        f"Leaf name '{leaf_name}' occurs {len(by_name)} "
                        f"times; disambiguated through the hierarchy: parent "
                        f"instance '{parent}' is of module type '{mod}', "
                        f"which defines this instance."
                    )
                    return self._result(fault_object, normalized, inst,
                                        pin_name, MappingConfidence.HIGH,
                                        evidence)
                if resolved:
                    evidence.append(
                        f"Leaf name '{leaf_name}' occurs {len(by_name)} "
                        f"times; the hierarchy narrowed it to "
                        f"{len(resolved)} candidate(s) but not to one."
                    )

        by_name = self._by_name.get(leaf, [])

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

    def _resolve_by_hierarchy(self, inst_path: str
                              ) -> List[Tuple[str, Instance]]:
        """Disambiguate a repeated leaf name using its ancestor path.

        For ``.../clkblk/bootfsm/foo_ctrl/state_out_reg`` the leaf may exist in
        many modules, but only one of them is the module type of an instance
        named ``foo_ctrl`` whose own parent chain also matches the path.
        Candidates that fail that chain are discarded.

        Args:
            inst_path: Normalised instance path (pin segment already removed).

        Returns:
            The surviving ``(module, instance)`` candidates.
        """
        parts = [p for p in inst_path.split("/") if p]
        if len(parts) < 2:
            return []
        candidates = self._by_name.get(parts[-1], [])
        return [(mod, inst) for mod, inst in candidates
                if self._ancestors_match(parts, len(parts) - 1, mod, 0)]

    def _ancestors_match(self, parts: List[str], idx: int, module: str,
                         depth: int) -> bool:
        """True when *module* is reachable through the path above *idx*.

        Walks upwards: the module holding the instance at ``parts[idx]`` must
        be the cell type of an instance named ``parts[idx - 1]``, and so on.
        The walk stops once the path is exhausted or ``MAX_ANCESTOR_DEPTH``
        levels have agreed, which is enough to make a leaf name unique in
        practice.
        """
        if idx <= 0 or depth >= self.MAX_ANCESTOR_DEPTH:
            return True
        parent_name = parts[idx - 1]
        # The outermost component of a fault path is normally the top-level
        # MODULE (or design) name, which is instantiated nowhere -- so looking
        # for an instance of that name finds nothing. Treat a component that
        # names the module we have just walked up into as the root of the
        # chain. Without this, any repeated leaf name is unresolvable as soon
        # as the fault list quotes the full path, which is the usual case.
        if parent_name == module:
            return True
        for parent_module, parent_inst in self._by_name.get(parent_name, []):
            if parent_inst.cell_type != module:
                continue
            if self._ancestors_match(parts, idx - 1, parent_module, depth + 1):
                return True
        return False

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
            module_name=inst.module,
            pin_name=pin,
            evidence=evidence,
        )
