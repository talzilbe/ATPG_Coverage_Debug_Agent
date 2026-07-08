"""Constraint Impact Skill — identifies constraints with broad fan-out impact."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register

logger = logging.getLogger(__name__)


@register
class ConstraintImpactSkill(SkillBase):
    """Identify constraints whose signals appear in many coverage-loss faults.

    A constraint that forces a signal to a constant value may block
    controllability or observability for a large number of downstream cells.
    This skill counts how many AU/UO/UC faults are constraint-related and
    clusters them by the originating constraint signal.
    """

    skill_id = "constraint_impact"
    display_name = "Constraint Impact"
    description = (
        "Identifies constraints with broad downstream fan-out and flags "
        "those most likely to cause controllability or observability loss."
    )
    default_enabled = True

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        return {
            "min_faults": {
                "type": "int",
                "default": 2,
                "description": "Minimum number of linked faults to report a constraint",
            },
        }

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)
        min_faults = int(self.get_param("min_faults"))

        if not ctx.constraints:
            result.add_info("No constraints loaded — skill has nothing to analyse.")
            result.summary = "No constraints available."
            return result

        # Count how many fault results are constraint-related
        constraint_faults: Counter = Counter()
        constraint_types: Counter = Counter()
        for fr in ctx.fault_results:
            if fr.constraint_related:
                for fact in fr.observed_facts:
                    if fact.startswith("Constraint ("):
                        constraint_faults[fact] += 1
                if fr.controllability_issue:
                    constraint_types["controllability"] += 1
                if fr.observability_issue:
                    constraint_types["observability"] += 1

        if not constraint_faults:
            result.add_info("No constraint-related faults found in core analysis.")
            result.summary = "No constraint-linked coverage loss detected."
            return result

        # Flag high-impact constraints
        for constraint_key, count in constraint_faults.most_common():
            if count >= min_faults:
                result.add_finding(
                    title=f"High-impact constraint: {constraint_key}",
                    description=(
                        f"This constraint is linked to {count} coverage-loss fault(s). "
                        "It may be masking testability for a broad net cone."
                    ),
                    evidence=[f"Fault count: {count}"],
                    affected_objects=[constraint_key],
                    confidence="medium",
                    recommendation=(
                        "Review whether this constraint can be relaxed during "
                        "ATPG or whether additional X-bounding is needed."
                    ),
                )

        # Overall summary
        ctrl_count = constraint_types.get("controllability", 0)
        obsv_count = constraint_types.get("observability", 0)
        result.summary = (
            f"{len(constraint_faults)} constrained signal(s) affect coverage loss. "
            f"Controllability: {ctrl_count}, Observability: {obsv_count}."
        )
        result.add_info(result.summary)
        return result
