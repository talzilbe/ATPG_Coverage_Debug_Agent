"""Derived coverage statistics computed from the fault list alone.

A Tessent ``report_statistics`` listing is an aggregation of the fault list, so
the same breakdown can be produced locally with no extra input file: every
fault record already carries its dotted class, its stuck value and its
hierarchical path.

This module produces that breakdown (:func:`compute_statistics`) and then
applies the triage rule used during manual debug to pick the categories worth
investigating (:func:`select_categories`).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from ..knowledge.subclasses import (
    SubclassInfo,
    describe_subclass,
    is_coverage_loss_class,
)
from ..models import EvidenceSource, FaultRecord

logger = logging.getLogger(__name__)

#: Categories contributing less than this share of all faults are treated as a
#: sparse tail: real, but not worth debugging before the dominant categories.
DEFAULT_MIN_PCT = 1.0

#: Upper bound on how many categories to put forward at once, so triage stays
#: focused on the losses that actually move coverage.
DEFAULT_MAX_CATEGORIES = 5

#: When nothing clears ``min_pct``, fall back to this many largest categories
#: so a highly fragmented design still produces actionable output.
DEFAULT_FALLBACK_TOP_N = 3


@dataclass
class SubclassStat:
    """Aggregated counts for one dotted fault class.

    Attributes:
        subclass_id: Dotted class id, e.g. ``AU.TC``, or a bare family id when
            the fault list recorded no subtype.
        family: Coarse class (``AU`` / ``UC`` / ``UO`` / ``DS`` / ...).
        count: Number of faults in this category.
        pct: Share of the whole fault population, in percent.
        sa0: Faults with stuck-at-0.
        sa1: Faults with stuck-at-1.
        unknown_sa: Faults whose stuck value was not recorded.
    """

    subclass_id: str
    family: str
    count: int = 0
    pct: float = 0.0
    sa0: int = 0
    sa1: int = 0
    unknown_sa: int = 0

    @property
    def is_coverage_loss(self) -> bool:
        """True when this category represents debuggable coverage loss."""
        return is_coverage_loss_class(self.subclass_id)

    @property
    def sa_asymmetry(self) -> float:
        """Imbalance between stuck-at-0 and stuck-at-1 faults, 0.0 to 1.0.

        A value near zero means the polarities are evenly represented, which is
        the normal case. A high value means one polarity dominates, which
        points at a fixed value upstream of the fault sites.

        Returns ``0.0`` when no stuck values were recorded.
        """
        total = self.sa0 + self.sa1
        if total == 0:
            return 0.0
        return abs(self.sa0 - self.sa1) / total

    @property
    def info(self) -> Optional[SubclassInfo]:
        """Catalogue knowledge for this category, if any."""
        return describe_subclass(self.subclass_id)


@dataclass
class DerivedStatistics:
    """The full fault-class breakdown derived from a fault list.

    Attributes:
        total_faults: Every fault record parsed.
        detected_count: Faults in a detected class.
        loss_count: Faults in a debuggable coverage-loss class.
        other_count: Faults that are neither, e.g. tied or unrecognised.
        subclass_stats: Per-category statistics, largest first.
        source: Always :attr:`EvidenceSource.FAULT_LIST` — these numbers are a
            direct aggregation of parsed records, not an inference.
    """

    total_faults: int = 0
    detected_count: int = 0
    loss_count: int = 0
    other_count: int = 0
    subclass_stats: List[SubclassStat] = field(default_factory=list)
    source: EvidenceSource = EvidenceSource.FAULT_LIST

    @property
    def detected_pct(self) -> float:
        """Detected faults as a share of all faults, in percent.

        This is a plain ratio over the fault list. It is not a substitute for
        the tool's test-coverage figure, which accounts for fault collapsing
        and untestable-fault credit.
        """
        if self.total_faults == 0:
            return 0.0
        return 100.0 * self.detected_count / self.total_faults

    @property
    def loss_pct(self) -> float:
        """Coverage-loss faults as a share of all faults, in percent."""
        if self.total_faults == 0:
            return 0.0
        return 100.0 * self.loss_count / self.total_faults

    @property
    def loss_stats(self) -> List[SubclassStat]:
        """Only the debuggable coverage-loss categories, largest first."""
        return [s for s in self.subclass_stats if s.is_coverage_loss]

    def get(self, subclass_id: str) -> Optional[SubclassStat]:
        """Return statistics for *subclass_id*, or ``None`` if not present."""
        key = (subclass_id or "").strip().upper()
        for stat in self.subclass_stats:
            if stat.subclass_id == key:
                return stat
        return None

    def as_dict(self) -> Dict[str, object]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "total_faults": self.total_faults,
            "detected_count": self.detected_count,
            "loss_count": self.loss_count,
            "other_count": self.other_count,
            "detected_pct": round(self.detected_pct, 4),
            "loss_pct": round(self.loss_pct, 4),
            "source": self.source.value,
            "subclasses": [
                {
                    "subclass": s.subclass_id,
                    "family": s.family,
                    "count": s.count,
                    "pct": round(s.pct, 4),
                    "sa0": s.sa0,
                    "sa1": s.sa1,
                    "sa_asymmetry": round(s.sa_asymmetry, 4),
                }
                for s in self.subclass_stats
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "DerivedStatistics":
        """Rebuild statistics from an :meth:`as_dict` payload.

        Used when reloading a saved session, where the original fault list is
        no longer available but the aggregated breakdown was persisted.
        """
        stats = [
            SubclassStat(
                subclass_id=str(row.get("subclass", "")),
                family=str(row.get("family", "")),
                count=int(row.get("count", 0) or 0),
                pct=float(row.get("pct", 0.0) or 0.0),
                sa0=int(row.get("sa0", 0) or 0),
                sa1=int(row.get("sa1", 0) or 0),
            )
            for row in (data.get("subclasses") or [])
        ]
        return cls(
            total_faults=int(data.get("total_faults", 0) or 0),
            detected_count=int(data.get("detected_count", 0) or 0),
            loss_count=int(data.get("loss_count", 0) or 0),
            other_count=int(data.get("other_count", 0) or 0),
            subclass_stats=stats,
        )


@dataclass
class SelectedCategory:
    """A coverage-loss category chosen for investigation.

    Attributes:
        stat: The underlying statistics.
        rank: 1-based position, largest fault count first.
        reason: Why this category was selected, for the audit trail.
        clusters: Its ``cluster.ClusterReport``, once clustering has run.
        verdict: Its ``scoring.CategoryVerdict``, once scoring has run.
        attribution: Its ``attribution.Attribution``, for the categories whose
            blocking structure can be traced through the netlist.
        reachability: Its ``reachability.ReachabilityProfile``, for the
            categories whose faults were aborted rather than proven untestable.
    """

    stat: SubclassStat
    rank: int
    reason: str
    clusters: Any = None
    verdict: Any = None
    attribution: Any = None
    reachability: Any = None

    @property
    def subclass_id(self) -> str:
        return self.stat.subclass_id


def compute_statistics(faults: Iterable[FaultRecord]) -> DerivedStatistics:
    """Aggregate *faults* into a per-class and per-subclass breakdown.

    Args:
        faults: Parsed fault records.

    Returns:
        A :class:`DerivedStatistics` with categories ordered by fault count,
        descending. An empty input yields zeroed statistics rather than an
        error.
    """
    faults = list(faults)
    total = len(faults)

    counters: Dict[str, SubclassStat] = {}
    for fault in faults:
        key = fault.dotted_class
        stat = counters.get(key)
        if stat is None:
            stat = SubclassStat(
                subclass_id=key,
                family=fault.fault_class.value,
            )
            counters[key] = stat
        stat.count += 1
        sa = fault.sa_key
        if sa == "sa0":
            stat.sa0 += 1
        elif sa == "sa1":
            stat.sa1 += 1
        else:
            stat.unknown_sa += 1

    stats = sorted(counters.values(), key=lambda s: (-s.count, s.subclass_id))
    for stat in stats:
        stat.pct = (100.0 * stat.count / total) if total else 0.0

    detected = sum(s.count for s in stats
                   if s.family in ("DS", "DI", "RE"))
    loss = sum(s.count for s in stats if s.is_coverage_loss)

    result = DerivedStatistics(
        total_faults=total,
        detected_count=detected,
        loss_count=loss,
        other_count=total - detected - loss,
        subclass_stats=stats,
    )
    logger.info(
        "Derived statistics: %d fault(s) across %d categorie(s); "
        "%d detected, %d coverage-loss.",
        total, len(stats), detected, loss,
    )
    return result


def select_categories(
    stats: DerivedStatistics,
    min_pct: float = DEFAULT_MIN_PCT,
    max_categories: int = DEFAULT_MAX_CATEGORIES,
    fallback_top_n: int = DEFAULT_FALLBACK_TOP_N,
) -> List[SelectedCategory]:
    """Pick the coverage-loss categories worth debugging.

    Applies the triage rule used during manual debug: keep only coverage-loss
    categories with faults present, prefer those above *min_pct*, and cap the
    result so attention stays on the dominant losses. When nothing clears the
    threshold the largest categories are returned anyway, flagged as a sparse
    tail, so a fragmented design still produces usable output.

    Args:
        stats: Output of :func:`compute_statistics`.
        min_pct: Minimum share of the fault population to qualify.
        max_categories: Maximum number of categories to return.
        fallback_top_n: How many to return when none clear *min_pct*.

    Returns:
        Selected categories ranked by fault count, largest first.
    """
    candidates = [s for s in stats.loss_stats if s.count > 0]
    if not candidates:
        return []

    above = [s for s in candidates if s.pct >= min_pct]
    if above:
        chosen = above[:max_categories]
        reason = f"contributes {{pct:.2f}}% of all faults (>= {min_pct}%)"
        sparse = False
    else:
        chosen = candidates[:fallback_top_n]
        reason = (
            f"largest remaining coverage-loss category; no category reaches "
            f"{min_pct}% so the loss is spread thinly"
        )
        sparse = True

    selected = []
    for rank, stat in enumerate(chosen, start=1):
        text = reason.format(pct=stat.pct) if not sparse else reason
        selected.append(SelectedCategory(stat=stat, rank=rank, reason=text))
    logger.info("Selected %d coverage-loss categorie(s) for triage.",
                len(selected))
    return selected


def enrich_categories(selected: List[SelectedCategory],
                      faults: Iterable[FaultRecord]) -> List[SelectedCategory]:
    """Attach hierarchy clustering and a scored verdict to each category.

    Clustering answers where each category's faults concentrate; scoring turns
    that, plus the stuck-at split, into a reproducible actionability verdict.
    Both are filled in place so the categories stay a single object the rest
    of the pipeline can pass around.

    Args:
        selected: Categories from :func:`select_categories`.
        faults: The full fault population.

    Returns:
        The same list, with ``clusters`` and ``verdict`` populated.
    """
    # Imported here so the statistics module stays free of analysis imports at
    # module load, keeping the dependency direction one-way.
    from .cluster import cluster_faults
    from .scoring import score_category

    if not selected:
        return selected

    wanted = {c.subclass_id for c in selected}
    grouped: Dict[str, List[FaultRecord]] = {key: [] for key in wanted}
    for fault in faults:
        key = fault.dotted_class
        if key in grouped:
            grouped[key].append(fault)

    for category in selected:
        category.clusters = cluster_faults(
            grouped.get(category.subclass_id, []),
            label=category.subclass_id)
        category.verdict = score_category(category.stat, category.clusters)
    return selected


def counter_from(faults: Iterable[FaultRecord]) -> Counter:
    """Return a ``Counter`` of dotted class ids, for lightweight callers."""
    return Counter(f.dotted_class for f in faults)


def subtract_statistics(base: DerivedStatistics,
                        removed: Iterable[FaultRecord]) -> DerivedStatistics:
    """Return *base* with *removed* faults taken out and totals recomputed.

    Used when an analyst waives faults: the triage and fix plan must reflect
    the remaining population, otherwise the report keeps recommending work on
    a category that was just written off.

    Args:
        base: Statistics for the full population.
        removed: The fault records being excluded.

    Returns:
        Fresh statistics. Categories emptied by the exclusion are dropped
        entirely rather than shown as zero rows.
    """
    removed = list(removed)
    if not removed:
        return base

    deltas = compute_statistics(removed)
    kept: List[SubclassStat] = []
    for stat in base.subclass_stats:
        gone = deltas.get(stat.subclass_id)
        count = stat.count - (gone.count if gone else 0)
        if count <= 0:
            continue
        kept.append(SubclassStat(
            subclass_id=stat.subclass_id,
            family=stat.family,
            count=count,
            sa0=max(0, stat.sa0 - (gone.sa0 if gone else 0)),
            sa1=max(0, stat.sa1 - (gone.sa1 if gone else 0)),
            unknown_sa=max(0, stat.unknown_sa - (gone.unknown_sa if gone else 0)),
        ))

    kept.sort(key=lambda s: (-s.count, s.subclass_id))
    total = sum(s.count for s in kept)
    for stat in kept:
        stat.pct = (100.0 * stat.count / total) if total else 0.0

    detected = sum(s.count for s in kept if s.family in ("DS", "DI", "RE"))
    loss = sum(s.count for s in kept if s.is_coverage_loss)
    return DerivedStatistics(
        total_faults=total,
        detected_count=detected,
        loss_count=loss,
        other_count=total - detected - loss,
        subclass_stats=kept,
    )
