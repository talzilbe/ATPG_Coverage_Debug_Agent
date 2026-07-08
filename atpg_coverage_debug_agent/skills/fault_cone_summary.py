"""Fault Cone Summary Skill — compact upstream/downstream cone summaries."""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register

logger = logging.getLogger(__name__)


@register
class FaultConeSummarySkill(SkillBase):
    """Summarise fan-in/fan-out cone statistics for coverage-loss faults.

    Reports the distribution of cone sizes and flags faults with unusually
    small or large cones — which may indicate structural isolation issues.
    """

    skill_id = "fault_cone_summary"
    display_name = "Fault Cone Summary"
    description = (
        "Provides compact upstream/downstream cone size statistics for "
        "coverage-loss faults and flags structural isolation anomalies."
    )
    default_enabled = True

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        return {
            "zero_fanin_threshold": {
                "type": "int",
                "default": 5,
                "description": "Flag if more than this many faults have zero fan-in",
            },
            "zero_fanout_threshold": {
                "type": "int",
                "default": 5,
                "description": "Flag if more than this many faults have zero fan-out",
            },
            "large_cone_size": {
                "type": "int",
                "default": 50,
                "description": "Fan-out size considered 'large' for flagging",
            },
        }

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)

        if not ctx.fault_results:
            result.add_info("No fault results to summarise.")
            result.summary = "No fault results available."
            return result

        zero_fi_threshold = int(self.get_param("zero_fanin_threshold"))
        zero_fo_threshold = int(self.get_param("zero_fanout_threshold"))
        large_cone = int(self.get_param("large_cone_size"))

        fan_in_sizes = []
        fan_out_sizes = []
        zero_fi_faults = []
        zero_fo_faults = []
        large_fo_faults = []

        for fr in ctx.fault_results:
            fi = len(fr.fan_in)
            fo = len(fr.fan_out)
            fan_in_sizes.append(fi)
            fan_out_sizes.append(fo)

            if fi == 0:
                zero_fi_faults.append(fr.fault.fault_object)
            if fo == 0:
                zero_fo_faults.append(fr.fault.fault_object)
            if fo >= large_cone:
                large_fo_faults.append(fr.fault.fault_object)

        total = len(fan_in_sizes)
        avg_fi = sum(fan_in_sizes) / total if total else 0
        avg_fo = sum(fan_out_sizes) / total if total else 0
        max_fi = max(fan_in_sizes, default=0)
        max_fo = max(fan_out_sizes, default=0)

        result.add_info(
            f"Fan-in:  avg={avg_fi:.1f}, max={max_fi}, "
            f"zero={len(zero_fi_faults)} ({100*len(zero_fi_faults)//total if total else 0}%)"
        )
        result.add_info(
            f"Fan-out: avg={avg_fo:.1f}, max={max_fo}, "
            f"zero={len(zero_fo_faults)} ({100*len(zero_fo_faults)//total if total else 0}%)"
        )

        # Flag zero fan-in
        if len(zero_fi_faults) > zero_fi_threshold:
            result.add_finding(
                title=f"{len(zero_fi_faults)} faults have zero fan-in",
                description=(
                    f"{len(zero_fi_faults)} coverage-loss faults could not be "
                    "correlated to any netlist driver. This typically means the "
                    "fault object path did not match any extracted instance, or "
                    "the netlist is incomplete."
                ),
                evidence=[
                    f"Zero fan-in: {len(zero_fi_faults)} faults",
                    f"Threshold: {zero_fi_threshold}",
                ],
                affected_objects=zero_fi_faults[:10],
                confidence="high",
                recommendation=(
                    "Check that the netlist file covers the full partition, not "
                    "just a sub-module extract. Verify fault path hierarchy "
                    "matches the netlist top-level instance name."
                ),
            )
            result.add_warning(
                f"{len(zero_fi_faults)} fault(s) have zero fan-in — "
                "netlist coverage may be incomplete."
            )

        # Flag zero fan-out
        if len(zero_fo_faults) > zero_fo_threshold:
            result.add_finding(
                title=f"{len(zero_fo_faults)} faults have zero fan-out",
                description=(
                    f"{len(zero_fo_faults)} coverage-loss faults have no "
                    "observable fan-out path. These signals may be dangling "
                    "or their outputs are not used."
                ),
                evidence=[f"Zero fan-out: {len(zero_fo_faults)} faults"],
                affected_objects=zero_fo_faults[:10],
                confidence="medium",
                recommendation=(
                    "Check if these signals drive off-module outputs or are "
                    "tied to unused ports. They may be legitimately AU."
                ),
            )

        # Flag large fan-out (high observability concern)
        if large_fo_faults:
            result.add_finding(
                title=f"{len(large_fo_faults)} faults with large fan-out (≥{large_cone})",
                description=(
                    f"{len(large_fo_faults)} fault(s) drive {large_cone}+ "
                    "downstream cells. If any of these are unobserved, the "
                    "coverage impact is amplified."
                ),
                evidence=[f"Large fan-out faults: {len(large_fo_faults)}"],
                affected_objects=large_fo_faults[:5],
                confidence="medium",
                recommendation=(
                    "These high-fan-out nodes may benefit from dedicated "
                    "observation points in the test mode."
                ),
            )

        result.summary = (
            f"{total} fault(s) — "
            f"avg fan-in={avg_fi:.1f}, avg fan-out={avg_fo:.1f}; "
            f"{len(zero_fi_faults)} zero-fan-in, {len(zero_fo_faults)} zero-fan-out."
        )
        return result
