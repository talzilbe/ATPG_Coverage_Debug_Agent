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
from .recommend import build_recommendations
from .statistics import (
    enrich_categories,
    select_categories,
    subtract_statistics,
)
from .summarizer import Summarizer

_LOSS_CLASSES = ("AU", "UO", "UC")


def apply_exclusions(report: AnalysisReport,
                     excluded_classes: Iterable[str] = (),
                     excluded_ids: Iterable[str] = (),
                     excluded_subtypes: Iterable[str] = (),
                     note: str = "") -> AnalysisReport:
    """Return a new report with the given faults excluded and summary recomputed.

    Args:
        report:            The base (unedited) report.
        excluded_classes:  Coarse fault-class codes to drop entirely
            (e.g. ``["AU"]`` removes every AU fault regardless of subtype).
        excluded_ids:      Specific fault-object ids/paths to drop.
        excluded_subtypes: Dotted fault sub-class tokens to drop
            (e.g. ``["AU.NOFAULTS", "AU.TC"]``) matched against the fault's
            ``raw_class_token``.
        note:              Analyst note stored on and shown in the report.
    """
    ex_classes = {c.strip().upper() for c in (excluded_classes or ()) if c}
    ex_ids = {i for i in (excluded_ids or ()) if i}
    ex_subtypes = {s.strip().upper() for s in (excluded_subtypes or ()) if s}

    kept = []
    removed = 0
    removed_faults = []
    for r in report.fault_results:
        cls = r.fault.fault_class.value
        subtype = (r.fault.raw_class_token or cls).upper()
        if (cls in ex_classes or r.fault.fault_object in ex_ids
                or subtype in ex_subtypes):
            removed += 1
            removed_faults.append(r.fault)
            continue
        kept.append(r)

    kept_faults = [r.fault for r in kept]
    constraints = report.constraints or []
    summarizer = Summarizer(kept_faults, kept, constraints)
    new_summary = summarizer.summary(list(report.summary.warnings))
    patterns = summarizer.patterns()

    # The unresolved-cause breakdown is derived from the netlist, which is not
    # re-walked here. Carry it over only while the unmapped population is
    # untouched; otherwise drop it rather than show a stale split.
    if new_summary.unmapped_count == report.summary.unmapped_count:
        new_summary.unresolved_causes = dict(report.summary.unresolved_causes)

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

    # Waived faults must also leave the triage, so the fix plan stops
    # recommending work on a category the analyst has written off.
    base_stats = getattr(report, "statistics", None)
    if base_stats is not None:
        edited.statistics = subtract_statistics(base_stats, removed_faults)
        edited.selected_categories = enrich_categories(
            select_categories(edited.statistics), kept_faults)
        edited.recommendations = build_recommendations(
            edited.statistics, edited.selected_categories)

    edited.edits = {
        "excluded_classes": sorted(ex_classes),
        "excluded_subtypes": sorted(ex_subtypes),
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
    if edits.get("excluded_subtypes"):
        parts.append("excluded subtypes: "
                     + ", ".join(edits["excluded_subtypes"]))
    if edits.get("excluded_ids"):
        parts.append(f"{len(edits['excluded_ids'])} fault(s) excluded by id")
    if edits.get("removed_count"):
        parts.append(f"{edits['removed_count']} fault(s) removed total")
    return "; ".join(parts)
