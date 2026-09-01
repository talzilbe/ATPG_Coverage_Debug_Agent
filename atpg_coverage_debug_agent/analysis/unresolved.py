"""Diagnose *why* fault objects failed to map onto the netlist.

An ``unresolved_connectivity`` verdict is not a root cause -- it is a hole in
the evidence. Until the hole is explained, every root cause assigned to those
sites is unprovable, and their fan-in/fan-out is unknown rather than zero.

Three causes are distinguishable from the netlist alone, and they need
different fixes:

``absent_leaf``
    The path's parent module *is* in the netlist, but the leaf instance is not
    inside it. The cell's model is missing -- a black box, an unexpanded macro
    or a library model that was never read in.
``ambiguous``
    The leaf name exists, several times, and the hierarchy did not narrow it
    to one. The netlist is complete; the *path* could not be pinned down.
``outside_scope``
    No segment of the path exists in the netlist at all. The fault list and
    the netlist describe different blocks, or the wrong partition was loaded.

Anything left over is reported as ``undetermined`` rather than forced into a
bucket.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from ..models import MappingConfidence

logger = logging.getLogger(__name__)

ABSENT_LEAF = "absent_leaf"
AMBIGUOUS = "ambiguous"
OUTSIDE_SCOPE = "outside_scope"
UNDETERMINED = "undetermined"

#: Representative fault paths kept per group, quoted verbatim.
MAX_SAMPLES = 3

_MEANING = {
    ABSENT_LEAF: (
        "The parent module is in the netlist but does not contain this "
        "instance. The cell model is missing: read the library/black-box "
        "model in, or supply the netlist that expands it."
    ),
    AMBIGUOUS: (
        "The instance name exists several times and the hierarchy did not "
        "narrow it to one. Supply the full hierarchical path, or the parent "
        "module type, for these faults."
    ),
    OUTSIDE_SCOPE: (
        "No part of this path exists in the loaded netlist. The fault list "
        "and the netlist cover different blocks."
    ),
    UNDETERMINED: (
        "Could not be attributed to a specific cause; inspect these paths "
        "individually."
    ),
}


@dataclass
class UnresolvedGroup:
    """Unmapped faults sharing one cause and one cell/module family."""

    cause: str
    family: str
    count: int = 0
    samples: List[str] = field(default_factory=list)
    #: Deepest path segment that *did* exist in the netlist, when any.
    deepest_known: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cause": self.cause,
            "meaning": _MEANING.get(self.cause, ""),
            "family": self.family,
            "count": self.count,
            "deepest_known_ancestor": self.deepest_known,
            "samples": list(self.samples),
        }


@dataclass
class UnresolvedDiagnosis:
    """Why the unmapped faults are unmapped."""

    analysed: int = 0
    unresolved: int = 0
    by_cause: Dict[str, int] = field(default_factory=dict)
    groups: List[UnresolvedGroup] = field(default_factory=list)

    @property
    def note(self) -> str:
        if not self.unresolved:
            return "Every coverage-loss fault mapped onto the netlist."
        parts = ", ".join(f"{cause}={n}"
                          for cause, n in sorted(self.by_cause.items(),
                                                 key=lambda kv: -kv[1]))
        return (f"{self.unresolved} of {self.analysed} coverage-loss fault(s) "
                f"did not map onto the netlist ({parts}). Their fan-in, "
                f"fan-out and scan status are UNKNOWN, not zero -- no root "
                f"cause on these sites is provable until the mapping is "
                f"fixed.")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "analysed": self.analysed,
            "unresolved": self.unresolved,
            "by_cause": dict(self.by_cause),
            "note": self.note,
            "groups": [g.as_dict() for g in self.groups],
        }


def diagnose_unresolved(fault_results: Iterable[Any], netlist: Any,
                        max_groups: int = 20) -> UnresolvedDiagnosis:
    """Group unmapped faults by the reason the mapping failed.

    Args:
        fault_results: ``FaultAnalysisResult`` objects.
        netlist: The parsed netlist, or ``None``.
        max_groups: Largest number of groups to return.

    Returns:
        An :class:`UnresolvedDiagnosis`.
    """
    diagnosis = UnresolvedDiagnosis()
    results = list(fault_results or [])
    diagnosis.analysed = len(results)
    if netlist is None or not getattr(netlist, "modules", None):
        return diagnosis

    known_instances = {inst.name
                       for module in netlist.modules.values()
                       for inst in module.instances.values()}
    instance_types = {inst.name: inst.cell_type
                      for module in netlist.modules.values()
                      for inst in module.instances.values()}

    buckets: Dict[tuple, UnresolvedGroup] = {}
    causes: Counter = Counter()

    for result in results:
        if result.mapping.confidence is not MappingConfidence.UNRESOLVED:
            continue
        diagnosis.unresolved += 1
        path = result.mapping.normalized_object or ""
        parts = [p for p in path.split("/") if p]
        leaf = parts[-1] if parts else path

        deepest = _deepest_known(parts, known_instances)
        if result.mapping.candidates:
            cause = AMBIGUOUS
        elif deepest is None:
            cause = OUTSIDE_SCOPE
        elif leaf not in known_instances:
            cause = ABSENT_LEAF
        else:
            cause = UNDETERMINED

        family = instance_types.get(deepest or "", "") or _family(parts)
        causes[cause] += 1
        key = (cause, family)
        group = buckets.get(key)
        if group is None:
            group = UnresolvedGroup(cause=cause, family=family,
                                    deepest_known=deepest)
            buckets[key] = group
        group.count += 1
        if len(group.samples) < MAX_SAMPLES:
            # Verbatim: a rewritten path will not resolve when pasted back.
            group.samples.append(result.fault.fault_object)

    diagnosis.by_cause = dict(causes)
    diagnosis.groups = sorted(buckets.values(), key=lambda g: -g.count)[
        :max_groups]
    return diagnosis


def _deepest_known(parts: List[str], known: set) -> Optional[str]:
    """Deepest path segment that names an instance we actually parsed."""
    for segment in reversed(parts):
        if segment in known:
            return segment
    return None


def _family(parts: List[str]) -> str:
    """Best-effort family label when no ancestor could be identified."""
    if not parts:
        return "(unknown)"
    return parts[-2] if len(parts) >= 2 else parts[-1]
