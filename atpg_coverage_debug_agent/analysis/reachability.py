"""Structural profiling of aborted-fault sites.

When ATPG aborts a fault it has not proven the fault untestable — it ran out of
search budget. Deciding what to do next depends on *why* the search was hard,
and the tool's own ``analyze_fault`` answers that by reporting five things:
sequential depth, whether the fault can be activated, how many observation
points exist, how much the detection paths reconverge, and its own summary.

This module estimates the first four of those from the netlist alone, and
applies the same decision rules to reach the same verdicts. Crucially, those
verdicts call for *opposite* actions — raising the abort limit helps a narrow
observability bottleneck and is wasted effort on reconvergent complexity — so
separating them is what makes the recommendation worth anything.

These are structural approximations. Real ATPG reasons about Boolean
satisfiability across the whole cone; fan-out counting cannot. Every verdict
here is labelled as inference and stays subordinate to a real tool run.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from ..models import EvidenceSource, MappingConfidence
from .attribution import Attributor, _is_scan_cell, _SEQ_CELL

logger = logging.getLogger(__name__)

#: Levels of fan-out to explore when looking for observation points.
DEFAULT_MAX_DEPTH = 8

#: Stop expanding a cone after this many instances. Wide cones are common in
#: real designs and the extra nodes do not change the verdict.
MAX_CONE_NODES = 2000

#: Sequential elements on the path to an observation point above which the
#: propagation distance is the dominant problem.
SEQ_DEPTH_HIGH = 8

#: Observation points at or below this count is a narrow channel rather than a
#: genuine gap.
OBS_BOTTLENECK = 2

#: Re-merge points in the cone above which the search is fighting a heavily
#: reconvergent structure, which no amount of extra abort budget will fix.
RECONVERGENCE_HIGH = 5

#: Fan-in levels walked above a site to find the fan-out stem that may
#: enclose it.
STEM_DEPTH = 3

#: Gates on the site's propagation path whose SIDE input is fed from that same
#: stem. At or above this, a bottleneck verdict is really reconvergence seen
#: from inside the cone.
ENCLOSING_RECONVERGENCE_HIGH = 2

#: When this share of profiled sites agrees, the signature is taken to
#: describe the whole category rather than just the sites examined.
CONSENSUS_SHARE = 0.8

#: Representative fault paths kept per signature, quoted verbatim.
DEFAULT_SAMPLES = 3

#: A limitation of measuring reconvergence by counting re-merge points in the
#: fan-out cone: it is only visible from a site ABOVE the fan-out. From a site
#: already inside the cone there is genuinely one narrow path onward, so the
#: same structure reads as a bottleneck. Real ATPG sees it either way, through
#: the side-input values each path demands, which structural tracing does not
#: model. The two verdicts call for OPPOSITE fixes -- a bottleneck is helped by
#: more abort budget, reconvergent complexity is not -- so this is stated
#: wherever a bottleneck verdict is reported rather than left in a comment.
RECONVERGENCE_BLIND_SPOT = (
    "Reconvergence is counted where fan-out paths re-merge, which is only "
    "visible from a site above the fan-out. A site inside a reconvergent cone "
    "sees one narrow path; to catch that, the side inputs of that path are "
    f"checked against the fan-out stem up to {STEM_DEPTH} levels above the "
    "site, and a match is reported as reconvergent complexity. A stem further "
    "up is not examined, so a bottleneck verdict can still be reconvergence: "
    "if so, a higher abort limit will not help and the design bypass is the "
    "fix to look at."
)

#: Verdict id -> (human label, fixes it calls for).
SIGNATURES: Dict[str, Dict[str, Any]] = {
    "sequential_depth_explosion": {
        "label": "Sequential depth explosion",
        "meaning": ("The fault effect has to travel through many sequential "
                    "elements before anything can capture it."),
        "fix_ids": ["seq_observe_point", "aab_observe_cutpoint"],
    },
    "low_controllability": {
        "label": "Low controllability",
        "meaning": ("A constant reaches the fault site, so the value needed "
                    "to activate the fault cannot be justified."),
        "fix_ids": ["aab_control_cutpoint"],
    },
    "hard_observability_gap": {
        "label": "Hard observability gap",
        "meaning": ("No scan cell or output is reachable downstream, so the "
                    "fault effect has nowhere to go."),
        "fix_ids": ["aab_observe_cutpoint"],
    },
    "reconvergent_complexity": {
        "label": "Reconvergent complexity",
        "meaning": ("Observation paths exist but they re-merge repeatedly, "
                    "forcing ATPG to satisfy many conditions at once."),
        "fix_ids": ["aab_design_bypass"],
    },
    "observability_bottleneck": {
        "label": "Observability bottleneck",
        "meaning": ("Only one or two observation points exist, so the search "
                    "has very little room to work with."),
        "fix_ids": ["aab_abort_limit", "aab_observe_cutpoint"],
    },
    "no_structural_blocker": {
        "label": "No structural blocker found",
        "meaning": ("The cone looks healthy, so the abort was most likely "
                    "search budget rather than design structure."),
        "fix_ids": ["aab_abort_limit"],
    },
    "cone_truncated": {
        "label": "Cone too large to profile",
        "meaning": ("The fan-out cone exceeded this tool's search bound, so "
                    "the observation-point and re-merge counts are lower "
                    "bounds rather than measurements. No observability "
                    "verdict can be drawn from them."),
        # Deliberately empty: recommending a fix on an admittedly incomplete
        # measurement is exactly the guess this analysis exists to avoid.
        "fix_ids": [],
    },
}


@dataclass
class SiteProfile:
    """Structural measurements for one fault site.

    Attributes:
        instance: The netlist instance the fault mapped onto.
        activatable: Whether a value can plausibly be justified at the site.
            ``None`` when it could not be determined.
        observation_points: Scan cells or outputs reachable downstream.
        sequential_depth: Sequential elements on the shortest path to the
            nearest observation point.
        reconvergence: Points in the fan-out cone where paths re-merge.
        truncated: True when the cone hit ``MAX_CONE_NODES`` and the counts
            above are therefore lower bounds, not measurements.
        signature: The verdict id these measurements imply.
    """

    instance: str = ""
    activatable: Optional[bool] = None
    observation_points: int = 0
    sequential_depth: int = 0
    reconvergence: int = 0
    truncated: bool = False
    #: Path gates whose side input comes from the stem above the site.
    enclosing_reconvergence: int = 0
    signature: str = "no_structural_blocker"

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "instance": self.instance,
            "activatable": self.activatable,
            "observation_points": self.observation_points,
            "sequential_depth": self.sequential_depth,
            "reconvergence": self.reconvergence,
            "enclosing_reconvergence": self.enclosing_reconvergence,
            "truncated": self.truncated,
            "signature": self.signature,
        }


@dataclass
class ReachabilityProfile:
    """How one coverage-loss category's fault sites look structurally.

    Attributes:
        subclass_id: The category profiled.
        analysed: Faults examined.
        profiled: Faults that mapped onto a netlist object and were measured.
        unresolved_mapping: Faults skipped because they never mapped.
        signatures: Verdict id -> how many sites showed it.
        dominant: The most common verdict id.
        dominant_share: Its share of profiled sites, 0.0 to 1.0.
        consensus: True when the dominant signature is shared widely enough to
            describe the category as a whole.
        samples: Verdict id -> representative fault paths, verbatim.
        preferred_fix_ids: Fixes the dominant signature calls for.
        truncated_sites: Sites whose fan-out cone exceeded the search bound,
            so their counts are lower bounds.
        note: Human-readable summary.
    """

    subclass_id: str
    analysed: int = 0
    profiled: int = 0
    unresolved_mapping: int = 0
    signatures: Dict[str, int] = field(default_factory=dict)
    dominant: str = ""
    dominant_share: float = 0.0
    consensus: bool = False
    samples: Dict[str, List[str]] = field(default_factory=dict)
    preferred_fix_ids: List[str] = field(default_factory=list)
    truncated_sites: int = 0
    note: str = ""
    #: The structural verdict before an analyze_fault measurement replaced it.
    estimated_dominant: str = ""
    #: analyze_fault sample summary when the tool measured this category.
    measured: Optional[Dict[str, Any]] = None
    _note_before_measurement: Optional[str] = None

    @property
    def dominant_label(self) -> str:
        entry = SIGNATURES.get(self.dominant)
        return entry["label"] if entry else "Unknown"

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "subclass": self.subclass_id,
            "analysed": self.analysed,
            "profiled": self.profiled,
            "unresolved_mapping": self.unresolved_mapping,
            "truncated_sites": self.truncated_sites,
            "dominant": self.dominant,
            "dominant_label": self.dominant_label,
            "dominant_share": round(self.dominant_share, 4),
            "consensus": self.consensus,
            "signatures": [
                {
                    "signature": key,
                    "label": SIGNATURES.get(key, {}).get("label", key),
                    "meaning": SIGNATURES.get(key, {}).get("meaning", ""),
                    "count": count,
                    "samples": list(self.samples.get(key, [])),
                }
                for key, count in sorted(self.signatures.items(),
                                         key=lambda kv: (-kv[1], kv[0]))
            ],
            "note": self.note,
            "estimated_dominant": self.estimated_dominant or self.dominant,
            "measured": dict(self.measured) if self.measured else None,
            "source": (EvidenceSource.TOOL_REPORT.value if self.measured
                       else EvidenceSource.STRUCTURAL_INFERENCE.value),
            "caveat": ("Estimated by walking the netlist. Real ATPG reasons "
                       "about Boolean satisfiability across the whole cone, "
                       "which structural tracing cannot reproduce."),
            "known_blind_spot": RECONVERGENCE_BLIND_SPOT,
        }


class StructuralProfiler:
    """Measures fan-in and fan-out structure around fault sites.

    Profiles are memoised per instance: coverage-loss faults concentrate onto
    shared cones, so the same walk would otherwise repeat many times over.
    """

    def __init__(self, connectivity: Any, attributor: Optional[Attributor] = None,
                 max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        self.conn = connectivity
        self.attributor = attributor or Attributor(connectivity, ())
        self.max_depth = max_depth
        self._cache: Dict[str, SiteProfile] = {}
        self._by_name: Dict[str, str] = {}
        for key in getattr(connectivity, "instances", {}):
            self._by_name.setdefault(key.split("::", 1)[-1], key)

    def _is_observation_point(self, inst: Any) -> bool:
        """True when *inst* can capture a fault effect for later scan-out."""
        cell = inst.cell_type or ""
        return _is_scan_cell(cell)

    def _explore_downstream(self, start_key: str) -> Dict[str, Any]:
        """Walk the fan-out cone, counting observe points and re-merges."""
        observe_points = 0
        best_depth: Optional[int] = None
        predecessors: Dict[str, Set[str]] = {}
        seen: Set[str] = {start_key}
        queue = deque([(start_key, 0)])

        while queue and len(seen) < MAX_CONE_NODES:
            key, seq_depth = queue.popleft()
            if seq_depth > self.max_depth:
                continue
            for child in self.conn.downstream(key):
                predecessors.setdefault(child, set()).add(key)
                if child in seen:
                    continue
                seen.add(child)
                inst = self.conn.instances.get(child)
                if inst is None:
                    continue
                cell = inst.cell_type or ""
                is_seq = bool(_SEQ_CELL.search(cell))
                depth = seq_depth + (1 if is_seq else 0)

                if self._is_observation_point(inst):
                    observe_points += 1
                    if best_depth is None or depth < best_depth:
                        best_depth = depth
                    # A scan cell captures the effect; nothing beyond it is
                    # part of this propagation problem.
                    continue
                queue.append((child, depth))

        reconvergence = sum(1 for parents in predecessors.values()
                            if len(parents) > 1)
        return {
            "observation_points": observe_points,
            "sequential_depth": best_depth if best_depth is not None else 0,
            "reconvergence": reconvergence,
            "cone": seen,
            # The walk stopped early, so every count above is a floor. Callers
            # must not read "0 observation points" as "no observation points".
            "truncated": bool(queue) and len(seen) >= MAX_CONE_NODES,
        }

    def _bounded_walk(self, start: Iterable[str], step, depth: int) -> Set[str]:
        found: Set[str] = set()
        frontier = set(start)
        for _ in range(depth):
            nxt: Set[str] = set()
            for key in frontier:
                for other in step(key):
                    if other not in found:
                        found.add(other)
                        nxt.add(other)
                    if len(found) >= MAX_CONE_NODES:
                        return found
            frontier = nxt
        return found

    def _enclosing_reconvergence(self, start_key: str, cone: Set[str]) -> int:
        """Count gates on the site's fan-out whose side input shares its stem.

        From inside a reconvergent cone the site sees one narrow path; the
        reconvergence shows up as the side inputs of that path being driven
        from the same fan-out stem that feeds the site. Walk up a few levels
        to the stem, walk its fan-out forward, and count path gates that take
        a side input from that region.
        """
        stem = self._bounded_walk([start_key], self.conn.upstream, STEM_DEPTH)
        if not stem:
            return 0
        region = self._bounded_walk(stem, self.conn.downstream,
                                    STEM_DEPTH + self.max_depth)
        region -= {start_key}
        own_cone = set(cone) | {start_key}
        count = 0
        for gate in cone:
            if gate == start_key:
                continue
            drivers = set(self.conn.upstream(gate))
            on_path = drivers & own_cone
            side = drivers - own_cone
            if on_path and side & region:
                count += 1
        return count

    def profile(self, instance_name: Optional[str]) -> Optional[SiteProfile]:
        """Measure the structure around *instance_name*.

        Returns:
            A :class:`SiteProfile`, or ``None`` when the instance is not in
            the netlist.
        """
        key = self._by_name.get(instance_name or "")
        if key is None:
            return None
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        downstream = self._explore_downstream(key)
        blocked_by_constant = self.attributor.find_tie_source(instance_name)

        profile = SiteProfile(
            instance=instance_name or "",
            activatable=blocked_by_constant is None,
            observation_points=downstream["observation_points"],
            sequential_depth=downstream["sequential_depth"],
            reconvergence=downstream["reconvergence"],
            truncated=downstream["truncated"],
        )
        profile.signature = classify_site(profile)
        if profile.signature == "observability_bottleneck":
            # Only the bottleneck verdict is ambiguous with reconvergence seen
            # from inside the cone, so only it pays for the extra walk.
            profile.enclosing_reconvergence = self._enclosing_reconvergence(
                key, downstream["cone"])
            profile.signature = classify_site(profile)
        self._cache[key] = profile
        return profile


def classify_site(profile: SiteProfile) -> str:
    """Return the verdict id implied by a site's measurements.

    The order matters and follows the same precedence used when reading
    ``analyze_fault`` output: depth first, then activation, then whether any
    observation point exists at all, then how badly the paths reconverge.

    When the cone was truncated the counts are lower bounds. A threshold that
    has already been *exceeded* still holds, so those verdicts survive; a
    verdict that depends on a count being *small* does not, because more
    searching could only have found more. Those become ``cone_truncated``
    rather than a confident answer built on an admittedly partial walk.
    """
    if profile.sequential_depth >= SEQ_DEPTH_HIGH:
        return "sequential_depth_explosion"
    if profile.activatable is False:
        return "low_controllability"
    if profile.reconvergence >= RECONVERGENCE_HIGH:
        # Measured from the fan-in side too, and already over the threshold.
        if profile.observation_points == 0 and not profile.truncated:
            return "hard_observability_gap"
        return "reconvergent_complexity"
    if profile.truncated:
        return "cone_truncated"
    if profile.observation_points == 0:
        return "hard_observability_gap"
    if profile.observation_points <= OBS_BOTTLENECK:
        if profile.enclosing_reconvergence >= ENCLOSING_RECONVERGENCE_HIGH:
            return "reconvergent_complexity"
        return "observability_bottleneck"
    return "no_structural_blocker"

def profile_faults(results: Iterable[Any], profiler: StructuralProfiler,
                   subclass_id: str) -> ReachabilityProfile:
    """Profile every fault site in one category and summarise the verdicts.

    Args:
        results: ``FaultAnalysisResult`` objects for the category.
        profiler: A configured :class:`StructuralProfiler`.
        subclass_id: The category being profiled.

    Returns:
        A :class:`ReachabilityProfile`.
    """
    outcome = ReachabilityProfile(subclass_id=subclass_id)

    for result in results:
        outcome.analysed += 1
        mapping = result.mapping
        if mapping.confidence is MappingConfidence.UNRESOLVED:
            outcome.unresolved_mapping += 1
            continue

        profile = profiler.profile(mapping.instance_name)
        if profile is None:
            outcome.unresolved_mapping += 1
            continue

        outcome.profiled += 1
        if profile.truncated:
            outcome.truncated_sites += 1
        outcome.signatures[profile.signature] = \
            outcome.signatures.get(profile.signature, 0) + 1
        samples = outcome.samples.setdefault(profile.signature, [])
        if len(samples) < DEFAULT_SAMPLES:
            samples.append(result.fault.fault_object)

    _finalise(outcome)
    return outcome


def _finalise(outcome: ReachabilityProfile) -> None:
    """Set the dominant signature, preferred fixes and note."""
    if not outcome.signatures:
        outcome.note = (
            f"None of the {outcome.analysed} fault(s) examined could be "
            f"located in the netlist, so no structural profile was built."
            if outcome.analysed else "No faults to profile.")
        return

    dominant, count = max(outcome.signatures.items(),
                          key=lambda kv: (kv[1], kv[0]))
    outcome.dominant = dominant
    outcome.dominant_share = count / max(outcome.profiled, 1)
    outcome.consensus = outcome.dominant_share >= CONSENSUS_SHARE
    entry = SIGNATURES.get(dominant, {})
    outcome.preferred_fix_ids = list(entry.get("fix_ids", []))

    scope = ("so the whole category can be treated as one problem"
             if outcome.consensus
             else "though the category is structurally mixed, so treat the "
                  "rest separately")
    outcome.note = (
        f"{entry.get('label', dominant)}: {entry.get('meaning', '')} "
        f"This describes {count} of {outcome.profiled} profiled site(s) "
        f"({outcome.dominant_share:.0%}), {scope}.")

    if outcome.unresolved_mapping:
        outcome.note += (
            f" {outcome.unresolved_mapping} fault(s) could not be located in "
            f"the netlist and were left out.")

    if outcome.truncated_sites:
        outcome.note += (
            f" {outcome.truncated_sites} of {outcome.profiled} site(s) have a "
            f"fan-out cone larger than this tool searches "
            f"({MAX_CONE_NODES} nodes), so their observation-point and "
            f"re-merge counts are lower bounds; no observability verdict was "
            f"drawn for them.")

    if outcome.dominant == "observability_bottleneck":
        # These two verdicts call for opposite fixes, and the structural proxy
        # cannot always tell them apart, so say so where it matters.
        outcome.note += " " + RECONVERGENCE_BLIND_SPOT


#: Categories whose faults were aborted rather than proven untestable, and so
#: benefit from knowing which structural obstacle the search hit. AU.SEQ is
#: included for its measured sequential depth.
PROFILED_SUBCLASSES = ("UC.AAB", "UO.AAB", "UC", "UO", "AU.SEQ")


def profile_categories(selected: List[Any], fault_results: Iterable[Any],
                       connectivity: Any, constraints: Iterable[Any] = (),
                       max_depth: int = DEFAULT_MAX_DEPTH,
                       cache: Any = None) -> List[Any]:
    """Attach a :class:`ReachabilityProfile` to every aborted-fault category.

    Args:
        selected: ``SelectedCategory`` objects to enrich in place.
        fault_results: All coverage-loss analysis results.
        connectivity: The ``ConnectivityModel`` for the design.
        constraints: Parsed constraint records.
        max_depth: Fan-out levels to explore.
        cache: Optional ``AnalysisCache`` whose site profiles are reused.

    Returns:
        The same list, with ``reachability`` populated where applicable.
    """
    targets = {c.subclass_id for c in selected} & set(PROFILED_SUBCLASSES)
    if not targets or connectivity is None:
        return selected

    grouped: Dict[str, List[Any]] = {key: [] for key in targets}
    for result in fault_results or ():
        key = result.fault.dotted_class
        if key in grouped:
            grouped[key].append(result)

    profiler = StructuralProfiler(
        connectivity, Attributor(connectivity, constraints),
        max_depth=max_depth)
    use_cache = cache is not None and max_depth == DEFAULT_MAX_DEPTH
    if use_cache:
        cache.seed(profiler=profiler)
    for category in selected:
        if category.subclass_id not in targets:
            continue
        category.reachability = profile_faults(
            grouped.get(category.subclass_id, []), profiler,
            category.subclass_id)
        logger.info("Profiled %s: %d/%d site(s), dominant=%s (%.0f%%)",
                    category.subclass_id,
                    category.reachability.profiled,
                    category.reachability.analysed,
                    category.reachability.dominant or "none",
                    100 * category.reachability.dominant_share)
    if use_cache:
        cache.collect(profiler=profiler)
    return selected
