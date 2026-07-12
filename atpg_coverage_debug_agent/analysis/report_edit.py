"""Non-destructive report editing: exclude faults and annotate the summary.

A DFT engineer often wants to **waive** certain faults from a coverage report —
most commonly ``AU`` (ATPG-untestable) faults that are legitimately untestable —
or record an analyst note explaining a decision. :func:`apply_exclusions`
produces a *new* :class:`AnalysisReport` with the chosen faults removed and the
summary / pattern groups recomputed, leaving the original untouched so the edit
is fully reversible in the UI.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from ..models import AnalysisReport
from .summarizer import Summarizer

_LOSS_CLASSES = ("AU", "UO", "UC")


def apply_exclusions(report: AnalysisReport,
                     excluded_classes: Iterable[str] = (),
                     excluded_ids: Iterable[str] = (),
                     note: str = "") -> AnalysisReport:
    """Return a new report with the given faults excluded and summary recomputed.

    Args:
        report:           The base (unedited) report.
        excluded_classes: Fault-class codes to drop entirely (e.g. ``["AU"]``).
        excluded_ids:     Specific fault-object ids to drop.
        note:             Analyst note stored on and shown in the report.
    """
    ex_classes = {c.strip().upper() for c in (excluded_classes or ()) if c}
    ex_ids = {i for i in (excluded_ids or ()) if i}

    kept = []
    removed = 0
    for r in report.fault_results:
        cls = r.fault.fault_class.value
        if cls in ex_classes or r.fault.fault_object in ex_ids:
            removed += 1
            continue
        kept.append(r)

    kept_faults = [r.fault for r in kept]
    constraints = report.constraints or []
    summarizer = Summarizer(kept_faults, kept, constraints)
    new_summary = summarizer.summary(list(report.summary.warnings))
    patterns = summarizer.patterns()

    # Preserve detected-class counts from the original; recompute the
    # coverage-loss classes from the kept set, and reduce the total.
    merged_counts = dict(report.summary.class_counts)
    kept_counts = Counter(r.fault.fault_class.value for r in kept)
    for cls in _LOSS_CLASSES:
        merged_counts[cls] = kept_counts.get(cls, 0)
    for cls in ex_classes:
        merged_counts[cls] = kept_counts.get(cls, 0)
    new_summary.class_counts = merged_counts
    new_summary.total_faults = max(0, report.summary.total_faults - removed)
    new_summary.coverage_loss_count = len(kept)

    edited = AnalysisReport(
        summary=new_summary,
        fault_results=kept,
        pattern_groups=patterns,
        warnings=list(report.warnings),
    )
    edited.skill_results = report.skill_results
    edited.netlist = report.netlist
    edited.faults = kept_faults
    edited.constraints = report.constraints
    edited.adjacency = getattr(report, "adjacency", None)
    edited.sources = getattr(report, "sources", None)
    edited.investigation = getattr(report, "investigation", None)
    edited.edits = {
        "excluded_classes": sorted(ex_classes),
        "excluded_ids": sorted(ex_ids),
        "note": note or "",
        "removed_count": removed,
    }
    return edited


def edit_banner(edits: dict) -> str:
    """Return a short human-readable banner describing the applied edits."""
    if not edits:
        return ""
    parts = []
    if edits.get("excluded_classes"):
        parts.append("excluded classes: " + ", ".join(edits["excluded_classes"]))
    if edits.get("excluded_ids"):
        parts.append(f"{len(edits['excluded_ids'])} fault(s) excluded by id")
    if edits.get("removed_count"):
        parts.append(f"{edits['removed_count']} fault(s) removed total")
    return "; ".join(parts)
