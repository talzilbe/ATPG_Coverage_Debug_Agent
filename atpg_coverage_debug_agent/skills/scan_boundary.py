"""Scan Boundary Skill — scan / non-scan split of the coverage-loss sites.

Scan status comes only from pin evidence: the instantiation the analysis pass
read for each site (``FaultAnalysisResult.scan_cell_state``). Names are never
used to decide it. An optional naming pass is offered as a labelled hint for
where to look, never as a classification.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Dict, List

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register
from ..parser.verilog_parser import is_clock_pin, parse_instantiation

logger = logging.getLogger(__name__)

#: Only used by the opt-in naming hint; never decides scan status.
_NAMING_HINTS = {
    "clock_gate": re.compile(r"(?i)(icg|clkgate|gated_clk|clk_gate)"),
    "memory": re.compile(r"(?i)(sram|_mem_|_ram_)"),
    "jtag_or_boundary_scan": re.compile(r"(?i)(_bscan|_tap_|jtag)"),
}

MAX_LISTED = 10


def _listed(objects: List[str]) -> List[str]:
    shown = objects[:MAX_LISTED]
    if len(objects) > MAX_LISTED:
        shown.append(f"... ({MAX_LISTED} of {len(objects)} listed)")
    return shown


def _module_of(fr: Any) -> str:
    mapping = getattr(fr, "mapping", None)
    return (getattr(mapping, "module_name", "") or getattr(fr, "cell_type", "")
            or "?")


def _is_sequential(text: str, cache: Dict[str, Any]) -> Any:
    """True/False from the recorded pin list's clock pin; None if unreadable."""
    if not text:
        return None
    if text not in cache:
        inst = parse_instantiation(text)
        cache[text] = (None if inst is None or not inst.pins else
                       any(is_clock_pin(p.name) for p in inst.pins))
    return cache[text]


@register
class ScanBoundarySkill(SkillBase):
    """Scan / non-scan split of the coverage-loss sites, from pin evidence."""

    skill_id = "scan_boundary"
    display_name = "Scan Boundary (pin evidence)"
    description = (
        "Splits the coverage-loss sites into scan / non-scan / unresolved "
        "using the scan pins read from each site's instantiation, and lists "
        "scan cells wired to non-scan sequential logic. Never uses names to "
        "decide scan status."
    )
    default_enabled = True

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        return {
            "min_cluster_size": {
                "type": "int",
                "default": 2,
                "description": "Minimum faults in a group to report",
            },
            "naming_hints": {
                "type": "bool",
                "default": False,
                "description": ("Also list name matches (clock gate / memory "
                                "/ JTAG) as labelled hints, not evidence"),
            },
        }

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)
        min_size = int(self.get_param("min_cluster_size"))
        results = list(ctx.fault_results or [])
        if not results:
            result.add_info("No coverage-loss faults to examine.")
            result.summary = "No coverage-loss faults."
            return result

        by_state: Dict[str, List[Any]] = {"scan": [], "non_scan": [],
                                          "unknown": []}
        for fr in results:
            state = getattr(fr, "scan_cell_state", "unknown") or "unknown"
            by_state.setdefault(state, []).append(fr)

        # "No scan pins" is true of every gate; only a state element without
        # them is a non-scan cell.
        cache: Dict[str, Any] = {}
        non_scan, combinational, undecided = [], [], []
        for fr in by_state["non_scan"]:
            seq = _is_sequential(getattr(fr, "scan_evidence", ""), cache)
            (non_scan if seq else combinational if seq is False
             else undecided).append(fr)

        if len(non_scan) >= min_size:
            sample = next((fr for fr in non_scan
                           if getattr(fr, "scan_evidence", "")), None)
            modules = Counter(_module_of(fr) for fr in non_scan).most_common(5)
            result.add_finding(
                title=f"{len(non_scan)} loss site(s) on non-scan state elements",
                description=(
                    "The instantiation read for each of these sites has a "
                    "clock pin but no scan-data-in / shift-enable pins, so "
                    "the flop cannot be loaded or captured through a chain."),
                evidence=(
                    [f"cell/module {m}: {n} site(s)" for m, n in modules]
                    + ([f"instantiation read: {sample.scan_evidence.strip()}"]
                       if sample else [])),
                affected_objects=_listed([fr.fault.fault_object
                                          for fr in non_scan]),
                confidence="high",
                recommendation=(
                    "Confirm with scan_status on a sample; if the cells "
                    "should be scanned, check scan insertion for that block."),
            )

        bordering = [fr for fr in by_state["scan"]
                     if getattr(fr, "scan_boundary_state", "") == "yes"]
        if len(bordering) >= min_size:
            result.add_finding(
                title=(f"{len(bordering)} scan cell(s) wired to non-scan "
                       "sequential logic"),
                description=(
                    "Scan cells (by pin evidence) whose immediate neighbour is "
                    "a sequential cell without scan pins: capture or launch "
                    "through that neighbour is not controlled by the chain."),
                evidence=[f"{len(bordering)} site(s) with a non-scan "
                          "sequential neighbour"],
                affected_objects=_listed([fr.fault.fault_object
                                          for fr in bordering]),
                confidence="medium",
                recommendation="Trace the neighbour with trace_path / "
                               "scan_status before proposing scan insertion.",
            )

        unknown = by_state["unknown"] + undecided
        if unknown:
            result.add_finding(
                title=f"{len(unknown)} loss site(s) with unresolved scan status",
                description=(
                    "No instantiation with readable pins was recorded for "
                    "these sites (usually unmapped faults), so scan status "
                    "cannot be determined. They are NOT evidence of non-scan "
                    "logic."),
                evidence=[f"{len(unknown)} of {len(results)} sites unresolved"],
                affected_objects=_listed([fr.fault.fault_object
                                          for fr in unknown]),
                confidence="insufficient",
                recommendation="Fix the mapping (diagnose_unresolved) first.",
            )

        if self.get_param("naming_hints"):
            for label, pattern in _NAMING_HINTS.items():
                hits = [fr.fault.fault_object for fr in results
                        if pattern.search(fr.fault.fault_object or "")]
                if len(hits) >= min_size:
                    result.add_finding(
                        title=f"Naming hint only: {len(hits)} '{label}' name(s)",
                        description=(
                            "Name match, not evidence. Use it to decide where "
                            "to look; scan status still needs scan_status."),
                        affected_objects=_listed(hits),
                        confidence="low",
                    )

        result.summary = (
            f"Scan status by pin evidence: {len(by_state['scan'])} scan cell, "
            f"{len(non_scan)} non-scan state element, {len(combinational)} "
            f"combinational (not applicable), {len(unknown)} unresolved "
            f"(of {len(results)} loss sites).")
        return result

