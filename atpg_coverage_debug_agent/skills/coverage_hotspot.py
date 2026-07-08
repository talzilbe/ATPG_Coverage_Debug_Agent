"""Coverage Hotspot Skill — clusters repeated AU/UO/UC failures."""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register

logger = logging.getLogger(__name__)


def _module_prefix(fault_object: str) -> str:
    """Extract a short module-level prefix from a hierarchical fault path."""
    parts = fault_object.lstrip("/").split("/")
    # Return top 2 hierarchy levels as module identifier
    return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]


def _cell_type_family(cell_type: str) -> str:
    """Strip numeric suffixes to group cell variants together."""
    if not cell_type:
        return "unknown"
    # Remove trailing _NNN or NNN
    return re.sub(r"_?\d+$", "", cell_type)


@register
class CoverageHotspotSkill(SkillBase):
    """Group repeated AU/UO/UC faults by module, instance, and cell-type family.

    Surfaces the top coverage hot-spots — areas with disproportionate numbers
    of undetected faults — so engineers can focus on the highest-impact fix.
    """

    skill_id = "coverage_hotspot"
    display_name = "Coverage Hotspot"
    description = (
        "Groups AU/UO/UC faults by module, instance, and cell-type family "
        "to surface the top coverage hot-spots for prioritised fixing."
    )
    default_enabled = True

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        return {
            "top_n": {
                "type": "int",
                "default": 10,
                "description": "Number of top hotspots to report per dimension",
            },
            "min_cluster": {
                "type": "int",
                "default": 3,
                "description": "Minimum faults in a cluster to report it",
            },
        }

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)
        top_n = int(self.get_param("top_n"))
        min_cluster = int(self.get_param("min_cluster"))

        if not ctx.fault_results:
            result.add_info("No fault results to cluster.")
            result.summary = "No fault results available."
            return result

        # Cluster by module prefix
        module_counter: Counter = Counter()
        module_faults: Dict[str, List[str]] = defaultdict(list)

        # Cluster by cell-type family
        cell_counter: Counter = Counter()
        cell_faults: Dict[str, List[str]] = defaultdict(list)

        # Cluster by root cause
        rc_counter: Counter = Counter()

        for fr in ctx.fault_results:
            obj = fr.fault.fault_object
            mod = _module_prefix(obj)
            module_counter[mod] += 1
            module_faults[mod].append(obj)

            cell = _cell_type_family(fr.cell_type or "")
            cell_counter[cell] += 1
            cell_faults[cell].append(obj)

            rc_counter[fr.root_cause.value] += 1

        total = len(ctx.fault_results)

        # --- Module-level hotspots ---
        for mod, count in module_counter.most_common(top_n):
            if count < min_cluster:
                continue
            pct = 100.0 * count / total if total else 0
            result.add_finding(
                title=f"Module hotspot: {mod} ({count} faults, {pct:.1f}%)",
                description=(
                    f"The module subtree '{mod}' contributes {count} "
                    f"coverage-loss fault(s) ({pct:.1f}% of all analysed)."
                ),
                evidence=[f"{count} AU/UO/UC faults in this subtree"],
                affected_objects=module_faults[mod][:5],
                confidence="high",
                recommendation=(
                    "Investigate the DFT methodology for this module. "
                    "Check scan insertion, constraint settings, and clock "
                    "gating interaction."
                ),
            )

        # --- Cell-type family hotspots ---
        for cell, count in cell_counter.most_common(top_n):
            if count < min_cluster or cell == "unknown":
                continue
            pct = 100.0 * count / total if total else 0
            result.add_finding(
                title=f"Cell-type hotspot: {cell} ({count} faults, {pct:.1f}%)",
                description=(
                    f"Cell type '{cell}' (or variants) accounts for {count} "
                    f"coverage-loss fault(s) ({pct:.1f}%). "
                    "This may indicate a systematic testability problem with "
                    "this cell type."
                ),
                evidence=[f"{count} faults on '{cell}' family"],
                affected_objects=cell_faults[cell][:5],
                confidence="medium",
                recommendation=(
                    "Check whether this cell type has known ATPG issues "
                    "(e.g., tied pins, unusual topology, or missing model)."
                ),
            )

        n_modules = sum(1 for c in module_counter.values() if c >= min_cluster)
        n_cells = sum(1 for c in cell_counter.values() if c >= min_cluster)
        result.summary = (
            f"{total} fault(s) clustered into {n_modules} module hotspot(s) "
            f"and {n_cells} cell-type hotspot(s)."
        )
        result.add_info(result.summary)
        return result
