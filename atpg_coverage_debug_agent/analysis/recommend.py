"""Ranked fix recommendations for the categories chosen during triage.

Recommendations join three things: the derived statistics
(:mod:`.statistics`), the subclass taxonomy and the fix catalogue
(:mod:`..knowledge`). Every recommendation carries its evidence with the source
of each fact attached, and a confidence level that degrades honestly when the
fault list gave no subtype to work with.

Nothing here predicts a coverage gain. Actions whose benefit can only be
established by re-running ATPG are marked ``requires_measurement`` and are
reported as hypotheses to test, not as expected improvements.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..knowledge.fixes import FixAction, fixes_for_subclass
from ..knowledge.subclasses import SUBCLASS_CATALOG, describe_subclass
from ..models import EvidenceSource, VerdictConfidence
from .statistics import DerivedStatistics, SelectedCategory, select_categories

logger = logging.getLogger(__name__)

#: Above this stuck-at imbalance a category is called strongly asymmetric,
#: which points at a fixed value upstream rather than a diffuse structural issue.
SA_ASYMMETRY_HIGH = 0.66

#: How many fix actions to put forward per category before the list stops
#: being a shortlist.
DEFAULT_MAX_FIXES_PER_CATEGORY = 3


@dataclass
class Recommendation:
    """One ranked fix proposal for one coverage-loss category.

    Attributes:
        rank: 1-based ordering across all recommendations.
        subclass_id: The category this addresses, e.g. ``AU.TC``.
        fault_count: Faults in that category.
        pct: Share of the whole fault population, in percent.
        fix: The catalogued action being proposed.
        confidence: How much weight this proposal should carry.
        evidence: Facts supporting it, each tagged with its source.
        caveats: Warnings that prevent a common misapplication.
    """

    rank: int
    subclass_id: str
    fault_count: int
    pct: float
    fix: FixAction
    confidence: VerdictConfidence
    evidence: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    actionable: str = "partial"
    hotspot: str = ""

    @property
    def title(self) -> str:
        return self.fix.title

    @property
    def requires_measurement(self) -> bool:
        """True when the benefit must be measured rather than estimated."""
        return self.fix.requires_measurement

    def as_dict(self) -> Dict[str, object]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "rank": self.rank,
            "subclass": self.subclass_id,
            "fault_count": self.fault_count,
            "pct": round(self.pct, 4),
            "fix_id": self.fix.fix_id,
            "title": self.fix.title,
            "rationale": self.fix.rationale,
            "preconditions": list(self.fix.preconditions),
            "commands": list(self.fix.commands),
            "expected_effect": self.fix.expected_effect,
            "effort": self.fix.effort,
            "risk": self.fix.risk,
            "requires_measurement": self.fix.requires_measurement,
            "confidence": self.confidence.value,
            "actionable": self.actionable,
            "hotspot": self.hotspot,
            "evidence": list(self.evidence),
            "caveats": list(self.caveats),
        }


def _tag(source: EvidenceSource, text: str) -> str:
    """Prefix *text* with its evidence source, e.g. ``[fault_list] ...``."""
    return f"[{source.value}] {text}"


def _confidence_for(category: SelectedCategory) -> VerdictConfidence:
    """Grade how much the evidence supports acting on *category*.

    When the category has been scored, that verdict wins: it weighs the
    clustering evidence as well as the class token. Otherwise the grade rests
    on the dotted subclass alone — the ATPG tool's own root-cause label, and
    the strongest signal available without clustering. A bare family id means
    no subtype was recorded, leaving only structural inference, which is
    reported at reduced confidence rather than dressed up.
    """
    verdict = getattr(category, "verdict", None)
    if verdict is not None:
        return verdict.confidence

    stat = category.stat
    exact = stat.subclass_id in SUBCLASS_CATALOG
    has_subtype = "." in stat.subclass_id

    if not describe_subclass(stat.subclass_id):
        return VerdictConfidence.INSUFFICIENT
    if exact and has_subtype:
        return VerdictConfidence.HIGH
    if has_subtype:
        # A subtype the catalogue does not cover; the family still applies.
        return VerdictConfidence.MEDIUM
    return VerdictConfidence.REDUCED


def _evidence_for(category: SelectedCategory) -> List[str]:
    """Assemble the supporting facts for *category*, each with its source."""
    stat = category.stat
    lines = [
        _tag(EvidenceSource.FAULT_LIST,
             f"{stat.count} fault(s) classified {stat.subclass_id}, "
             f"{stat.pct:.2f}% of the fault population."),
    ]

    if "." in stat.subclass_id:
        lines.append(_tag(
            EvidenceSource.FAULT_LIST,
            f"ATPG recorded the subtype '{stat.subclass_id}' itself, so the "
            f"category is the tool's own classification rather than an "
            f"inference by this analyzer."))
    else:
        lines.append(_tag(
            EvidenceSource.STRUCTURAL_INFERENCE,
            "The fault list recorded no subtype for this class, so the cause "
            "is not stated by the tool and must be established separately."))

    total_sa = stat.sa0 + stat.sa1
    if total_sa:
        detail = (f"stuck-at split is {stat.sa0} sa0 / {stat.sa1} sa1 "
                  f"(imbalance {stat.sa_asymmetry:.2f})")
        if stat.sa_asymmetry >= SA_ASYMMETRY_HIGH:
            detail += (" — a strong skew, consistent with a fixed value held "
                       "upstream of the fault sites")
        lines.append(_tag(EvidenceSource.FAULT_LIST, detail + "."))

    info = describe_subclass(stat.subclass_id)
    if info and info.primary_causes:
        lines.append(_tag(
            EvidenceSource.FAULT_LIST,
            f"Usual cause of {info.subclass_id}: {info.primary_causes[0]}"))

    clusters = getattr(category, "clusters", None)
    if clusters is not None and clusters.top is not None:
        top = clusters.top
        lines.append(_tag(
            EvidenceSource.CLUSTERING_HINT,
            f"{top.pct:.1f}% of these faults sit under '{top.prefix}' "
            f"(hierarchy depth {clusters.depth}) — where to look, not why."))

    verdict = getattr(category, "verdict", None)
    if verdict is not None:
        lines.append(_tag(
            EvidenceSource.CLUSTERING_HINT,
            f"Scored {verdict.actionable} to act on: {verdict.reason}"))

    attribution = getattr(category, "attribution", None)
    if attribution is not None and attribution.note:
        lines.append(_tag(EvidenceSource.STRUCTURAL_INFERENCE,
                          attribution.note))
        for source in attribution.tie_sources[:2]:
            value = f" (tied {source.tie_value})" if source.tie_value else ""
            lines.append(_tag(
                EvidenceSource.NETLIST,
                f"Constant driver '{source.driver}' [{source.cell_type}]"
                f"{value} reaches {source.count} fault(s)."))
        for source in attribution.constraint_sources[:2]:
            lines.append(_tag(
                EvidenceSource.CONSTRAINT_FILE,
                f"Constraint {source.kind} '{source.signal}' = "
                f"{source.value or '?'} reaches {source.count} fault(s)."))

    reachability = getattr(category, "reachability", None)
    if reachability is not None and reachability.note:
        lines.append(_tag(EvidenceSource.STRUCTURAL_INFERENCE,
                          reachability.note))
    return lines


def _caveats_for(category: SelectedCategory, fix: FixAction) -> List[str]:
    """Collect the warnings that apply to *fix* in *category*."""
    caveats: List[str] = []
    info = describe_subclass(category.subclass_id)
    if info and info.caveat:
        caveats.append(info.caveat)
    if fix.caveat:
        caveats.append(fix.caveat)
    if fix.requires_measurement:
        caveats.append(
            "The benefit of this action can only be established by re-running "
            "ATPG and comparing statistics. No gain is predicted here.")
    if info and info.auto_waive_hint:
        caveats.append(info.auto_waive_hint)
    return caveats


def _preferred_fix_ids(category: SelectedCategory) -> List[str]:
    """Collect fix ids that the traced evidence specifically points at.

    Both the blocking-source attribution and the structural site profile can
    identify a particular obstacle. Either one outranks the generic ordering
    by cost, because it rests on something measured about this design.
    """
    preferred: List[str] = []
    for source in (getattr(category, "attribution", None),
                   getattr(category, "reachability", None)):
        for fix_id in getattr(source, "preferred_fix_ids", []) or []:
            if fix_id not in preferred:
                preferred.append(fix_id)
    return preferred


def _ordered_fixes(category: SelectedCategory) -> List[FixAction]:
    """Return the category's fixes, promoting any the evidence points at.

    Cone tracing can identify *which* structure is blocking the faults — a
    configurable register rather than a hardwired tie, or a reconvergent cone
    rather than a narrow bottleneck. When it does, the matching fix moves to
    the front, because the generic ordering by cost no longer reflects what is
    actually known.
    """
    actions = fixes_for_subclass(category.subclass_id)
    preferred = _preferred_fix_ids(category)
    if not preferred:
        return actions

    rank = {fix_id: index for index, fix_id in enumerate(preferred)}
    return sorted(actions, key=lambda a: rank.get(a.fix_id, len(rank)))


def build_recommendations(
    stats: DerivedStatistics,
    selected: Optional[List[SelectedCategory]] = None,
    max_fixes_per_category: int = DEFAULT_MAX_FIXES_PER_CATEGORY,
) -> List[Recommendation]:
    """Produce ranked fix proposals for the selected coverage-loss categories.

    Ranking follows the order an experienced analyst would work in: the
    categories losing the most faults first, then those whose stuck-at skew
    gives a clearer root-cause signal, and within a category the cheapest
    actions before structural changes.

    Args:
        stats: Output of :func:`.statistics.compute_statistics`.
        selected: Categories to cover. Defaults to running
            :func:`.statistics.select_categories` over *stats*.
        max_fixes_per_category: Cap on proposals per category.

    Returns:
        Recommendations ordered best-first. Empty when there is no coverage
        loss to act on.
    """
    if selected is None:
        selected = select_categories(stats)
    if not selected:
        return []

    scored = []
    for category in selected:
        confidence = _confidence_for(category)
        evidence = _evidence_for(category)
        verdict = getattr(category, "verdict", None)
        concentration = (verdict.scores.concentration if verdict else 0.0)
        actions = _ordered_fixes(category)
        preferred = set(_preferred_fix_ids(category))
        for position, action in enumerate(actions[:max_fixes_per_category]):
            scored.append((
                -category.stat.count,
                -concentration,
                -category.stat.sa_asymmetry,
                # A fix the traced evidence points at outranks the generic
                # cheapest-first ordering.
                position if action.fix_id in preferred
                else action.feasibility_rank + len(preferred),
                category,
                action,
                confidence,
                evidence,
            ))

    scored.sort(key=lambda row: row[:4])

    recommendations: List[Recommendation] = []
    for rank, row in enumerate(scored, start=1):
        _, _, _, _, category, action, confidence, evidence = row
        verdict = getattr(category, "verdict", None)
        recommendations.append(Recommendation(
            rank=rank,
            subclass_id=category.subclass_id,
            fault_count=category.stat.count,
            pct=category.stat.pct,
            fix=action,
            confidence=confidence,
            evidence=list(evidence),
            caveats=_caveats_for(category, action),
            actionable=(verdict.actionable if verdict else "partial"),
            hotspot=(verdict.hotspot if verdict else ""),
        ))

    logger.info("Built %d recommendation(s) across %d categorie(s).",
                len(recommendations), len(selected))
    return recommendations


def explain_category(subclass_id: str) -> Dict[str, object]:
    """Return everything known about *subclass_id* as a plain dict.

    Args:
        subclass_id: A dotted class id such as ``AU.TC``.

    Returns:
        A dict describing the subclass and its catalogued fixes. When the id is
        not recognised the dict contains ``known: False`` and nothing else is
        asserted about it.
    """
    key = (subclass_id or "").strip().upper()
    info = describe_subclass(key)
    if info is None:
        return {
            "subclass": key,
            "known": False,
            "note": (
                "This class is not in the catalogue, so no meaning or fix can "
                "be stated for it."
            ),
        }
    return {
        "subclass": key,
        "known": True,
        "matched": info.subclass_id,
        "title": info.title,
        "family": info.family,
        "meaning": info.meaning,
        "primary_causes": list(info.primary_causes),
        "evidence_needed": list(info.evidence_needed),
        "caveat": info.caveat,
        "auto_waive_hint": info.auto_waive_hint,
        "fixes": [
            {
                "fix_id": f.fix_id,
                "title": f.title,
                "rationale": f.rationale,
                "preconditions": list(f.preconditions),
                "commands": list(f.commands),
                "expected_effect": f.expected_effect,
                "effort": f.effort,
                "risk": f.risk,
                "requires_measurement": f.requires_measurement,
                "caveat": f.caveat,
            }
            for f in fixes_for_subclass(key)
        ],
    }
