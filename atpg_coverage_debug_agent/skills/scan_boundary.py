"""Scan Boundary Detection Skill — identifies likely scan/non-scan boundaries."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, List

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register

logger = logging.getLogger(__name__)

# Cell-type patterns that typically live at scan/non-scan boundaries
_NON_SCAN_PATTERNS = [
    re.compile(r"(?i)(latch|dlat|sdff|sdffs|dff|flop)", ),
    re.compile(r"(?i)(sram|rom|ram|mem|array)", ),
    re.compile(r"(?i)(analog|phy|pll|osc|lvl|iso)", ),
]

# Instance name keywords that suggest non-scan or boundary cells
_NON_SCAN_NAME_PATTERNS = [
    re.compile(r"(?i)(_bscan|_tap|jtag|boundary_scan)"),
    re.compile(r"(?i)(glbdrvuclk|HFSBUF|glb_clk)"),
]

_CLOCK_GATE_PATTERNS = [
    re.compile(r"(?i)(icg|clkgate|cg_|gated_clk|clk_gate)"),
]


@register
class ScanBoundarySkill(SkillBase):
    """Detect likely scan/non-scan boundary cells in coverage-loss faults.

    Uses naming-convention heuristics to classify fault objects as likely
    scan flops, non-scan elements, or clock-gate boundary cells.
    Confidence is always marked as heuristic — results must be verified.
    """

    skill_id = "scan_boundary"
    display_name = "Scan Boundary Detection"
    description = (
        "Identifies likely scan/non-scan boundary cells using naming "
        "conventions and cell-type patterns. Confidence is heuristic."
    )
    default_enabled = True

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        return {
            "min_cluster_size": {
                "type": "int",
                "default": 2,
                "description": "Minimum faults in a cluster to report",
            },
        }

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)
        min_size = int(self.get_param("min_cluster_size"))

        # Categorise fault results by boundary type
        clusters: Dict[str, List[str]] = defaultdict(list)

        for fr in ctx.fault_results:
            obj = fr.fault.fault_object
            cell = (fr.cell_type or "").lower()
            inst = (fr.instance_name or obj).lower()

            category = self._classify(obj, cell, inst)
            if category:
                clusters[category].append(obj)

        if not clusters:
            result.add_info("No likely scan-boundary faults identified.")
            result.summary = "No scan boundary patterns detected."
            return result

        total = sum(len(v) for v in clusters.values())
        for category, objects in sorted(clusters.items(),
                                        key=lambda x: -len(x[1])):
            if len(objects) < min_size:
                continue
            result.add_finding(
                title=f"Likely {category} boundary ({len(objects)} faults)",
                description=(
                    f"{len(objects)} coverage-loss fault(s) appear near a "
                    f"'{category}' scan boundary. These may be structurally "
                    "unobservable due to the boundary between scan and non-scan domains."
                ),
                evidence=[f"{len(objects)} faults match '{category}' pattern"],
                affected_objects=objects[:10],
                confidence="low",
                recommendation=(
                    f"Review '{category}' cells in the design. If these are "
                    "intentional non-scan elements, add them to the don't-care list. "
                    "Otherwise, check scan insertion coverage."
                ),
            )
            result.add_warning(
                f"{len(objects)} fault(s) near '{category}' boundary — "
                "manual verification recommended."
            )

        result.summary = (
            f"{total} fault(s) show likely scan-boundary patterns "
            f"across {len(clusters)} category(ies). "
            "Confidence: heuristic (verify before acting)."
        )
        return result

    def _classify(self, obj: str, cell: str, inst: str) -> str:
        """Return a boundary category name, or empty string if no match."""
        for pat in _CLOCK_GATE_PATTERNS:
            if pat.search(obj) or pat.search(cell) or pat.search(inst):
                return "clock_gate"
        for pat in _NON_SCAN_NAME_PATTERNS:
            if pat.search(obj) or pat.search(inst):
                return "non_scan_cell"
        for pat in _NON_SCAN_PATTERNS:
            if pat.search(cell):
                return "non_scan_cell"
        # heuristic: paths containing 'sram' or 'mem'
        if re.search(r"(?i)(sram|_mem_|_ram_)", obj):
            return "memory_boundary"
        return ""
