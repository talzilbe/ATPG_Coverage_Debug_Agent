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
        unmapped = []

        for fr in ctx.fault_results:
            # An unmapped site has UNKNOWN connectivity, not zero: keep it out
            # of every cone statistic.
            if not getattr(fr, "connectivity_known", True):
                unmapped.append(fr.fault.fault_object)
                continue
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
            f"Cone statistics over {total} mapped site(s); "
            f"{len(unmapped)} unmapped site(s) excluded (connectivity unknown)."
        )
        result.add_info(
            f"Fan-in:  avg={avg_fi:.1f}, max={max_fi}, "
            f"zero={len(zero_fi_faults)} ({100*len(zero_fi_faults)//total if total else 0}%)"
        )
        result.add_info(
            f"Fan-out: avg={avg_fo:.1f}, max={max_fo}, "
            f"zero={len(zero_fo_faults)} ({100*len(zero_fo_faults)//total if total else 0}%)"
        )

        if unmapped:
            result.add_finding(
                title=f"{len(unmapped)} site(s) did not map onto the netlist",
                description=(
                    "These fault objects matched no netlist instance, so their "
                    "fan-in and fan-out are UNKNOWN (not zero) and they are "
                    "left out of the cone statistics. No structural claim can "
                    "be made about them until they map."
                ),
                evidence=[f"Unmapped: {len(unmapped)} of "
                          f"{len(unmapped) + total} loss sites"],
                affected_objects=_listed(unmapped),
                confidence="high",
                recommendation=(
                    "Run diagnose_unresolved for the cause (missing cell "
                    "model, ambiguous name, or a netlist covering a different "
                    "block)."
                ),
            )

        # Flag zero fan-in on MAPPED sites only
        if len(zero_fi_faults) > zero_fi_threshold:
            result.add_finding(
                title=f"{len(zero_fi_faults)} mapped site(s) have zero fan-in",
                description=(
                    f"{len(zero_fi_faults)} coverage-loss sites mapped onto "
                    "the netlist but have no driver inside it: an undriven "
                    "input, a port driven from outside the netlist, or a "
                    "cell whose pins were not classified."
                ),
                evidence=[
                    f"Zero fan-in (mapped): {len(zero_fi_faults)} sites",
                    f"Threshold: {zero_fi_threshold}",
                ],
                affected_objects=_listed(zero_fi_faults),
                confidence="medium",
                recommendation=(
                    "Check a sample with get_fault_detail / trace_path before "
                    "treating these as controllability loss."
                ),
            )
            result.add_warning(
                f"{len(zero_fi_faults)} mapped site(s) have zero fan-in."
            )

        # Flag zero fan-out
        if len(zero_fo_faults) > zero_fo_threshold:
            result.add_finding(
                title=f"{len(zero_fo_faults)} mapped site(s) have zero fan-out",
                description=(
                    f"{len(zero_fo_faults)} coverage-loss sites mapped onto "
                    "the netlist but drive nothing inside it. These signals "
                    "may be dangling or their outputs are not used."
                ),
                evidence=[f"Zero fan-out (mapped): {len(zero_fo_faults)} sites"],
                affected_objects=_listed(zero_fo_faults),
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
                affected_objects=_listed(large_fo_faults, 5),
                confidence="medium",
                recommendation=(
                    "These high-fan-out nodes may benefit from dedicated "
                    "observation points in the test mode."
                ),
            )

        result.summary = (
            f"{total} mapped site(s) — "
            f"avg fan-in={avg_fi:.1f}, avg fan-out={avg_fo:.1f}; "
            f"{len(zero_fi_faults)} zero-fan-in, {len(zero_fo_faults)} "
            f"zero-fan-out; {len(unmapped)} unmapped (excluded)."
        )
        return result


def _listed(objects, limit: int = 10):
    shown = list(objects[:limit])
    if len(objects) > limit:
        shown.append(f"... ({limit} of {len(objects)} listed)")
    return shown
