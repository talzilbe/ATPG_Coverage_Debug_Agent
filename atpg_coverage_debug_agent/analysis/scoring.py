"""Deterministic scoring of coverage-loss categories.

Turns clustering output and fault counts into a stable verdict: how
concentrated the loss is, whether the stuck-at split points at a fixed value,
how deep in the hierarchy it sits, and whether any of that is worth acting on.

The rules are fixed formulas with fixed thresholds, so the same inputs always
produce the same verdict. That matters more than sophistication here: a triage
result that shifts between runs cannot be reviewed or argued with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..knowledge.subclasses import SUBCLASS_CATALOG, describe_subclass
from ..models import VerdictConfidence

logger = logging.getLogger(__name__)

#: Score bands. Anything below :data:`BAND_LOW` is weak evidence; anything
#: above :data:`BAND_HIGH` is strong.
BAND_LOW = 0.33
BAND_HIGH = 0.66

#: Spread in the top clusters' percentages, above which the distribution is
#: no longer considered symmetric.
SYMMETRY_TOLERANCE_PCT = 8.0

#: A category contributing less than this share of all faults is a sparse tail.
NEGLIGIBLE_PCT = 1.0

#: Hierarchy depth treated as fully "deep" when normalising the depth score.
DEPTH_NORMALISER = 10.0

#: Below this many faults a category carries no statistical signal: one fault
#: is trivially 100% concentrated and 100% stuck-at skewed. Scoring such a
#: category would manufacture strong evidence out of nothing, so all factors
#: are left at zero instead.
MIN_FAULTS_FOR_SCORING = 10

#: Fix ids that do not constitute a real lever — proposing only these means
#: there is no concrete action available yet.
_WEAK_LEVERS = {"generic_review"}


def band(score: float) -> str:
    """Return ``low`` / ``medium`` / ``high`` for *score*."""
    if score < BAND_LOW:
        return "low"
    if score <= BAND_HIGH:
        return "medium"
    return "high"


def _stdev(values: List[float]) -> float:
    """Population standard deviation, 0.0 for fewer than two values."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5


@dataclass
class ScoreFactors:
    """The four measurements a verdict is built from, each 0.0 to 1.0.

    Attributes:
        concentration: Share of the category's faults in its three largest
            clusters. High means the loss has a focal point.
        symmetry: How evenly the top clusters are sized. High means the loss
            is spread uniformly, which usually indicates a structural or
            replicated cause rather than one broken block.
        sa_asymmetry: Stuck-at imbalance. High points at a value held fixed
            upstream of the fault sites.
        depth: How deep in the hierarchy the clustering had to go before the
            loss localised.
    """

    concentration: float = 0.0
    symmetry: float = 0.0
    sa_asymmetry: float = 0.0
    depth: float = 0.0

    @property
    def bands(self) -> Dict[str, str]:
        """Band label for each factor."""
        return {
            "concentration": band(self.concentration),
            "symmetry": band(self.symmetry),
            "sa_asymmetry": band(self.sa_asymmetry),
            "depth": band(self.depth),
        }

    @property
    def high_count(self) -> int:
        """How many factors scored in the high band."""
        return sum(1 for value in self.bands.values() if value == "high")

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "concentration": round(self.concentration, 4),
            "symmetry": round(self.symmetry, 4),
            "sa_asymmetry": round(self.sa_asymmetry, 4),
            "depth": round(self.depth, 4),
            "bands": self.bands,
        }


@dataclass
class CategoryVerdict:
    """The scored conclusion for one coverage-loss category.

    Attributes:
        subclass_id: The category scored, e.g. ``AU.TC``.
        scores: The four underlying measurements.
        patterns: Tags describing the shape of the loss.
        actionable: ``true``, ``partial`` or ``false``.
        reason: One sentence citing concentration, depth, asymmetry and
            feasibility in that order.
        confidence: How much weight the verdict should carry.
        route: The catalogue entry that matched, or ``generic``.
        hotspot: The dominant cluster prefix, when clustering was available.
    """

    subclass_id: str
    scores: ScoreFactors = field(default_factory=ScoreFactors)
    patterns: List[str] = field(default_factory=list)
    actionable: str = "partial"
    reason: str = ""
    confidence: VerdictConfidence = VerdictConfidence.REDUCED
    route: str = "generic"
    hotspot: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "subclass": self.subclass_id,
            "scores": self.scores.as_dict(),
            "patterns": list(self.patterns),
            "actionable": self.actionable,
            "reason": self.reason,
            "confidence": self.confidence.value,
            "route": self.route,
            "hotspot": self.hotspot,
        }


def compute_scores(stat: Any, clusters: Optional[Any] = None,
                   symmetry_tolerance_pct: float = SYMMETRY_TOLERANCE_PCT
                   ) -> ScoreFactors:
    """Measure the four score factors for one category.

    Args:
        stat: A ``SubclassStat`` for the category.
        clusters: Its ``ClusterReport``, or ``None`` when unavailable — the
            concentration, symmetry and depth factors then stay at zero.
        symmetry_tolerance_pct: Spread in cluster percentages treated as the
            boundary of an even distribution.

    Returns:
        The populated :class:`ScoreFactors`. Every factor stays at zero for a
        population below :data:`MIN_FAULTS_FOR_SCORING`, since none of these
        measurements mean anything at that size.
    """
    if getattr(stat, "count", 0) < MIN_FAULTS_FOR_SCORING:
        return ScoreFactors()

    factors = ScoreFactors(sa_asymmetry=getattr(stat, "sa_asymmetry", 0.0))
    if clusters is None or not getattr(clusters, "clusters", None):
        return factors

    factors.concentration = min(1.0, clusters.top_n_share(3))
    factors.depth = min(1.0, clusters.depth / DEPTH_NORMALISER)

    # Symmetry compares sibling clusters against each other. With fewer than
    # two there are no siblings, so the question does not apply — leaving the
    # score at zero rather than at a spurious 1.0 from a zero deviation.
    sibling_pcts = [c.pct for c in clusters.clusters[:5]]
    if len(sibling_pcts) < 2:
        return factors

    spread = _stdev(sibling_pcts)
    factors.symmetry = 1.0 - min(
        1.0, spread / max(symmetry_tolerance_pct, 1.0))
    return factors


def tag_patterns(stat: Any, factors: ScoreFactors,
                 negligible_pct: float = NEGLIGIBLE_PCT) -> List[str]:
    """Describe the shape of the loss as a list of tags."""
    bands = factors.bands
    tags: List[str] = []
    if getattr(stat, "count", 0) < MIN_FAULTS_FOR_SCORING:
        tags.append("low_population")
    if bands["concentration"] == "high":
        tags.append("concentrated_hotspot")
    if bands["symmetry"] in ("medium", "high"):
        tags.append("symmetric_distribution")
    if bands["sa_asymmetry"] == "high":
        tags.append("high_sa_asymmetry")
    if bands["depth"] in ("medium", "high"):
        tags.append("deep_logic_chain")
    if (getattr(stat, "pct", 0.0) < negligible_pct
            and bands["concentration"] == "low"):
        tags.append("sparse_tail")
    return tags


def _has_feasible_lever(subclass_id: str) -> bool:
    """True when the catalogue offers a real action, not just 'go look'."""
    info = describe_subclass(subclass_id)
    if info is None:
        return False
    return any(fix_id not in _WEAK_LEVERS for fix_id in info.fix_ids)


def _decide_actionability(factors: ScoreFactors, patterns: List[str],
                          feasible: bool) -> str:
    """Apply the actionability matrix to the scored evidence."""
    bands = factors.bands
    strong_signal = (bands["depth"] == "high"
                     or bands["sa_asymmetry"] == "high")

    if "sparse_tail" in patterns:
        return "false"
    if "low_population" in patterns:
        # Too small to justify effort, but too small to dismiss either.
        return "partial"
    if bands["concentration"] == "high" and strong_signal and feasible:
        return "true"
    if not feasible and all(v == "low" for v in bands.values()):
        return "false"
    return "partial"


def _build_reason(factors: ScoreFactors, clusters: Optional[Any],
                  feasible: bool, subclass_id: str,
                  low_population: bool = False) -> str:
    """Compose the one-sentence rationale, in the required evidence order."""
    bands = factors.bands
    parts = []

    if low_population:
        parts.append(f"too few faults to score (below "
                     f"{MIN_FAULTS_FOR_SCORING})")
    elif clusters is not None and clusters.clusters:
        parts.append(
            f"{bands['concentration']} concentration "
            f"({factors.concentration:.0%} of faults in the top 3 clusters)")
    else:
        parts.append("concentration unknown (no clustering available)")

    parts.append(f"{bands['depth']} hierarchy depth "
                 f"(level {clusters.depth if clusters else 0})")
    parts.append(f"{bands['sa_asymmetry']} stuck-at asymmetry "
                 f"({factors.sa_asymmetry:.2f})")
    parts.append("a catalogued fix applies" if feasible
                 else f"no concrete fix is catalogued for {subclass_id}")
    return "; ".join(parts) + "."


def _decide_confidence(factors: ScoreFactors, route: str,
                       patterns: List[str]) -> VerdictConfidence:
    """Grade the verdict from the route quality and the strength of evidence."""
    mapped = route != "generic"
    strong = factors.high_count >= 2

    if "sparse_tail" in patterns or "low_population" in patterns:
        return VerdictConfidence.REDUCED
    if mapped and strong:
        return VerdictConfidence.HIGH
    if mapped or strong:
        return VerdictConfidence.MEDIUM
    return VerdictConfidence.REDUCED


def score_category(stat: Any, clusters: Optional[Any] = None,
                   symmetry_tolerance_pct: float = SYMMETRY_TOLERANCE_PCT,
                   negligible_pct: float = NEGLIGIBLE_PCT) -> CategoryVerdict:
    """Score one coverage-loss category and return its verdict.

    Args:
        stat: A ``SubclassStat`` for the category.
        clusters: Its ``ClusterReport``, or ``None``.
        symmetry_tolerance_pct: Boundary of an even cluster distribution.
        negligible_pct: Share below which a category is a sparse tail.

    Returns:
        The :class:`CategoryVerdict`.
    """
    subclass_id = getattr(stat, "subclass_id", "")
    factors = compute_scores(stat, clusters, symmetry_tolerance_pct)
    patterns = tag_patterns(stat, factors, negligible_pct)
    feasible = _has_feasible_lever(subclass_id)

    # An exact catalogue hit means the ATPG tool named the cause itself; a
    # family fallback means only the coarse class is known.
    route = subclass_id if subclass_id in SUBCLASS_CATALOG else "generic"

    verdict = CategoryVerdict(
        subclass_id=subclass_id,
        scores=factors,
        patterns=patterns,
        actionable=_decide_actionability(factors, patterns, feasible),
        reason=_build_reason(factors, clusters, feasible, subclass_id,
                             low_population="low_population" in patterns),
        confidence=_decide_confidence(factors, route, patterns),
        route=route,
        hotspot=(clusters.top.prefix
                 if clusters is not None and clusters.top else ""),
    )
    logger.debug("Scored %s: actionable=%s confidence=%s patterns=%s",
                 subclass_id, verdict.actionable, verdict.confidence.value,
                 verdict.patterns)
    return verdict
