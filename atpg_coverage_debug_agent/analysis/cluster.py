"""Hierarchy clustering of coverage-loss faults.

Grouping faults by their hierarchy prefix answers *where* the loss is
concentrated. A category spread evenly across a design and a category where
90% of the faults sit under one block need completely different responses,
and the raw fault list does not make that visible.

The clustering is deliberately treated as a **pointer, not a diagnosis**: a
dominant prefix says where to look, never why the faults are there. Only the
subclass taxonomy and, ultimately, the ATPG tool itself can state a cause.

Everything here works from the fault list alone — no netlist traversal, no
extra input file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: Deepest hierarchy level auto-depth selection will consider.
MAX_DEPTH = 12

#: Auto-depth keeps descending while the largest cluster still holds at least
#: this share of the population. Below it, the grouping has fragmented and
#: stopped pointing anywhere useful.
MIN_TOP_SHARE = 0.20

#: Auto-depth also stops once the grouping produces more clusters than this,
#: since a list that long is no longer a shortlist.
MAX_CLUSTERS = 50

#: Representative fault paths kept per cluster, quoted verbatim.
DEFAULT_SAMPLES = 3


@dataclass
class Cluster:
    """One hierarchy prefix and the faults underneath it.

    Attributes:
        prefix: The shared hierarchy path, e.g. ``top/u_core/u_fifo``.
        depth: How many hierarchy levels the prefix spans.
        count: Faults under this prefix.
        pct: Share of the clustered population, in percent.
        sa0: Faults with stuck-at-0.
        sa1: Faults with stuck-at-1.
        samples: Representative fault paths, copied verbatim from the fault
            list so they can be pasted into a tool without editing.
    """

    prefix: str
    depth: int
    count: int = 0
    pct: float = 0.0
    sa0: int = 0
    sa1: int = 0
    samples: List[str] = field(default_factory=list)

    @property
    def sa_asymmetry(self) -> float:
        """Stuck-at imbalance within this cluster, 0.0 to 1.0."""
        total = self.sa0 + self.sa1
        if total == 0:
            return 0.0
        return abs(self.sa0 - self.sa1) / total

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "prefix": self.prefix,
            "depth": self.depth,
            "count": self.count,
            "pct": round(self.pct, 4),
            "sa0": self.sa0,
            "sa1": self.sa1,
            "sa_asymmetry": round(self.sa_asymmetry, 4),
            "samples": list(self.samples),
        }


@dataclass
class ClusterReport:
    """The clustering of one fault population at one hierarchy depth.

    Attributes:
        label: What was clustered, normally a dotted subclass id.
        depth: The hierarchy depth used.
        total_faults: Faults in the clustered population.
        clusters: Clusters ordered by fault count, largest first.
        depth_note: Why this depth was chosen, for the audit trail.
    """

    label: str = ""
    depth: int = 1
    total_faults: int = 0
    clusters: List[Cluster] = field(default_factory=list)
    depth_note: str = ""

    @property
    def top(self) -> Optional[Cluster]:
        """The largest cluster, or ``None`` when there are none."""
        return self.clusters[0] if self.clusters else None

    @property
    def top_share(self) -> float:
        """Share of the population in the largest cluster, 0.0 to 1.0."""
        if not self.clusters or not self.total_faults:
            return 0.0
        return self.clusters[0].count / self.total_faults

    def top_n_share(self, n: int = 3) -> float:
        """Share of the population in the *n* largest clusters, 0.0 to 1.0."""
        if not self.clusters or not self.total_faults:
            return 0.0
        return sum(c.count for c in self.clusters[:n]) / self.total_faults

    def as_dict(self, limit: int = 10) -> Dict[str, Any]:
        """Plain-dict view keeping only the *limit* largest clusters."""
        return {
            "label": self.label,
            "depth": self.depth,
            "depth_note": self.depth_note,
            "total_faults": self.total_faults,
            "cluster_count": len(self.clusters),
            "top_share": round(self.top_share, 4),
            "note": ("A dominant prefix shows where the faults concentrate. "
                     "It is not a root cause."),
            "clusters": [c.as_dict() for c in self.clusters[:limit]],
        }


def _components(path: str) -> List[str]:
    """Split a normalised hierarchy path into its components."""
    return [part for part in (path or "").split("/") if part]


def _cluster_at_depth(faults: List[Any], depth: int,
                      max_samples: int) -> List[Cluster]:
    """Group *faults* by their first *depth* hierarchy components."""
    buckets: Dict[str, Cluster] = {}
    for fault in faults:
        parts = _components(getattr(fault, "normalized_object", ""))
        if not parts:
            continue
        prefix = "/".join(parts[:depth])
        cluster = buckets.get(prefix)
        if cluster is None:
            cluster = Cluster(prefix=prefix, depth=min(depth, len(parts)))
            buckets[prefix] = cluster
        cluster.count += 1
        sa = getattr(fault, "sa_key", None)
        if sa == "sa0":
            cluster.sa0 += 1
        elif sa == "sa1":
            cluster.sa1 += 1
        if len(cluster.samples) < max_samples:
            # Verbatim, not normalised: these paths are meant to be pasted
            # into a tool, where a rewritten separator would not resolve.
            cluster.samples.append(getattr(fault, "fault_object", ""))

    clusters = sorted(buckets.values(), key=lambda c: (-c.count, c.prefix))
    total = sum(c.count for c in clusters)
    for cluster in clusters:
        cluster.pct = (100.0 * cluster.count / total) if total else 0.0
    return clusters


def choose_depth(faults: List[Any],
                 min_top_share: float = MIN_TOP_SHARE,
                 max_clusters: int = MAX_CLUSTERS,
                 max_depth: int = MAX_DEPTH) -> tuple:
    """Pick the hierarchy depth that best localises *faults*.

    Descends one level at a time and keeps the deepest grouping that still
    points somewhere: the largest cluster must hold at least *min_top_share*
    of the population, and the grouping must not fragment past
    *max_clusters*. Going deeper than that produces a long list of tiny
    clusters that no longer indicates where to look.

    Args:
        faults: The fault population to cluster.
        min_top_share: Minimum share the largest cluster must retain.
        max_clusters: Maximum number of clusters before the split is rejected.
        max_depth: Deepest level to consider.

    Returns:
        A tuple ``(depth, note)`` where *note* explains the choice.
    """
    if not faults:
        return 1, "no faults to cluster"

    deepest = max((len(_components(getattr(f, "normalized_object", "")))
                   for f in faults), default=1)
    limit = max(1, min(max_depth, deepest))

    best_depth = 1
    note = "hierarchy has a single level"
    for depth in range(1, limit + 1):
        clusters = _cluster_at_depth(faults, depth, max_samples=0)
        if not clusters:
            break
        total = sum(c.count for c in clusters)
        share = clusters[0].count / total if total else 0.0
        if depth > 1 and (share < min_top_share
                          or len(clusters) > max_clusters):
            note = (f"stopped at depth {best_depth}: depth {depth} would "
                    f"scatter the faults into {len(clusters)} cluster(s) with "
                    f"only {share:.0%} in the largest")
            break
        best_depth = depth
        note = (f"deepest level where the largest cluster still holds "
                f"{share:.0%} of the faults across {len(clusters)} cluster(s)")
    return best_depth, note


def cluster_faults(faults: Iterable[Any], label: str = "",
                   depth: Optional[int] = None,
                   max_samples: int = DEFAULT_SAMPLES) -> ClusterReport:
    """Cluster *faults* by hierarchy prefix.

    Args:
        faults: Fault records exposing ``normalized_object``, ``fault_object``
            and ``sa_key``.
        label: What is being clustered, normally a dotted subclass id.
        depth: Fixed hierarchy depth, or ``None`` to choose one automatically.
        max_samples: Representative paths to keep per cluster.

    Returns:
        A :class:`ClusterReport`. An empty population yields an empty report
        rather than an error.
    """
    faults = list(faults)
    if not faults:
        return ClusterReport(label=label, depth=1, total_faults=0,
                             depth_note="no faults to cluster")

    if depth is None:
        depth, note = choose_depth(faults)
    else:
        depth = max(1, int(depth))
        note = f"depth {depth} requested explicitly"

    clusters = _cluster_at_depth(faults, depth, max_samples)
    report = ClusterReport(
        label=label,
        depth=depth,
        total_faults=len(faults),
        clusters=clusters,
        depth_note=note,
    )
    logger.info("Clustered %d %s fault(s) into %d cluster(s) at depth %d.",
                len(faults), label or "coverage-loss", len(clusters), depth)
    return report


def drill_into(faults: Iterable[Any], prefix: str, extra_depth: int = 1,
               max_samples: int = DEFAULT_SAMPLES) -> ClusterReport:
    """Re-cluster only the faults under *prefix*, one or more levels deeper.

    Args:
        faults: The same population that produced the parent clustering.
        prefix: The cluster prefix to descend into.
        extra_depth: How many further levels to expand.
        max_samples: Representative paths to keep per cluster.

    Returns:
        A :class:`ClusterReport` covering only the matching faults. When the
        prefix matches nothing, the report is empty and says so.
    """
    key = (prefix or "").strip().strip("/")
    if not key:
        return ClusterReport(label=prefix, depth=1,
                             depth_note="no prefix supplied")

    matching = [
        f for f in faults
        if getattr(f, "normalized_object", "").startswith(key + "/")
        or getattr(f, "normalized_object", "") == key
    ]
    if not matching:
        return ClusterReport(label=prefix, depth=1,
                             depth_note=f"no faults found under '{prefix}'")

    depth = len(_components(key)) + max(1, int(extra_depth))
    report = cluster_faults(matching, label=prefix, depth=depth,
                            max_samples=max_samples)
    report.depth_note = (f"expanded '{prefix}' by {extra_depth} level(s) "
                         f"across {len(matching)} fault(s)")
    return report
