"""DFT/ATPG Coverage Debug skill — wraps the ``dft-atpg-debug`` guidance.

This is a *built-in* skill (auto-discovered by the registry) so the
``dft-atpg-debug`` methodology always appears in the **Skills** tab. Its full
guidance (the ``SKILL.md`` document) is exposed via the *View content* button,
and when it runs it produces a concise coverage-loss diagnosis summary that
follows the skill's output format.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from typing import List, Optional

from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register
from ..models import RootCause

logger = logging.getLogger(__name__)

# Canonical location of the authored SKILL.md (best-effort load for content).
_SKILL_MD_PATHS = (
    "/nfs/site/disks/olevy2_wa01/hmlsoc_a0_ww23.5_clone_10440_SOC_SPEC_FIX/"
    ".github/skills/dft-atpg-debug/SKILL.md",
)

_FALLBACK_CONTENT = """# dft-atpg-debug

Root-cause ATPG stuck-at coverage loss from a gate-level netlist, a Tessent
fault list (AU/UO/UC faults) and the ATPG constraint dofile.

## Procedure (summary)
1. Parse the fault list and compute the fault-class statistics (DS/DI vs
   AU/UO/UC) and the estimated structural coverage.
2. Map each coverage-loss fault object to its netlist instance and cell type.
3. Build the fan-in / fan-out cone for each fault site.
4. Correlate against the ATPG constraints (forced / disabled / tied signals).
5. Classify the root cause: constraint controllability/observability loss,
   scan-to-non-scan boundary, non-scan propagation, tied/constant hardware,
   clock/reset/test-enable blocking, structural masking, or unresolved
   connectivity.
6. Group faults by root cause and module to find the dominant hotspots.
7. Recommend an actionable fix (add_input_constraints, add_clocks, USC PDL,
   or a waiver) for each root cause, ranked by fault-reduction impact.

## Output format
A — Executive fault-statistics table.
B — Coverage-loss summary table (module / classes / count / root cause / fix).
C — Per-root-cause debug narratives with signal paths.
D — Final diagnosis with a prioritised action list and projected coverage.
"""


def _load_skill_content() -> tuple[str, Optional[str]]:
    for path in _SKILL_MD_PATHS:
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read(), path
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("Could not read SKILL.md at %s: %s", path, exc)
    return _FALLBACK_CONTENT, None


_CONTENT, _SOURCE = _load_skill_content()


@register
class DftAtpgDebugSkill(SkillBase):
    """Coverage-loss root-cause skill following the ``dft-atpg-debug`` method."""

    skill_id = "dft_atpg_debug"
    display_name = "DFT/ATPG Coverage Debug"
    description = (
        "Root-cause ATPG stuck-at coverage loss (AU/UO/UC) from the netlist, "
        "fault list and constraints, following the dft-atpg-debug methodology."
    )
    default_enabled = True

    #: Full guidance shown by the Skills tab "View content" button.
    _content = _CONTENT
    #: Source path (when the authored SKILL.md was found on disk).
    _source_path = _SOURCE

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)

        results = list(ctx.fault_results or [])
        if not results:
            result.add_info("No coverage-loss faults to diagnose.")
            result.summary = "No AU/UO/UC faults present."
            return result

        summary = ctx.summary
        total = getattr(summary, "total_faults", 0) or 0
        counts = dict(getattr(summary, "class_counts", {}) or {})
        detected = sum(counts.get(c, 0) for c in ("DS", "DI"))
        loss = getattr(summary, "coverage_loss_count", 0) or len(results)
        cov = (100.0 * detected / total) if total else 0.0

        result.add_finding(
            title="Fault statistics summary",
            description=(
                f"{total:,} total faults; {detected:,} detected; "
                f"{loss:,} coverage-loss (AU/UO/UC). "
                f"Estimated structural coverage ~{cov:.1f}%."
            ),
            evidence=[f"{cls}: {n:,}" for cls, n in
                      Counter(counts).most_common()],
            confidence="high",
            recommendation=(
                "Target the dominant root-cause group below to recover the "
                "largest share of coverage."
            ),
        )

        rc_counter: Counter = Counter(r.root_cause for r in results)
        for rc, count in rc_counter.most_common(5):
            sample = next((r for r in results if r.root_cause == rc), None)
            rec = (sample.recommended_step if sample else "") or (
                "Review the boundary structurally; file a waiver if "
                "architecturally non-testable."
            )
            share = 100.0 * count / loss if loss else 0.0
            result.add_finding(
                title=f"Root cause: {self._rc_label(rc)} "
                      f"({count:,} faults, {share:.0f}%)",
                description=(
                    sample.inferred_conclusions[0]
                    if sample and sample.inferred_conclusions
                    else "Structural condition prevents test generation."
                ),
                affected_objects=[
                    r.fault.fault_object for r in results
                    if r.root_cause == rc
                ][:5],
                confidence="high" if share >= 40 else "medium",
                recommendation=rec,
            )

        top_rc, top_count = rc_counter.most_common(1)[0]
        result.summary = (
            f"~{cov:.1f}% coverage; primary gap: {self._rc_label(top_rc)} "
            f"({top_count:,} faults)."
        )
        return result

    @staticmethod
    def _rc_label(rc: RootCause) -> str:
        return rc.value.replace("_", " ").title()
