"""Summarisation and repeated-pattern detection, plus the orchestration entry.

:func:`build_report` is the single high-level call that ties parsers, the
connectivity model, the mapper and the root-cause engine together into an
:class:`AnalysisReport`.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import List, Optional, Tuple

from ..models import (
    AnalysisReport,
    AnalysisSummary,
    ConstraintRecord,
    FaultAnalysisResult,
    FaultClass,
    FaultRecord,
    PatternGroup,
    RootCause,
)
from ..parser.verilog_parser import VerilogNetlist
from .attribution import attribute_categories
from .connectivity import ConnectivityModel
from .mapper import FaultMapper
from .reachability import profile_categories
from .recommend import build_recommendations
from .root_cause import RootCauseEngine
from .statistics import compute_statistics, enrich_categories, select_categories
from .unresolved import diagnose_unresolved

logger = logging.getLogger(__name__)


class Summarizer:
    """Builds the executive summary and repeated-pattern groups."""

    def __init__(self, faults: List[FaultRecord],
                 results: List[FaultAnalysisResult],
                 constraints: List[ConstraintRecord]) -> None:
        self.faults = faults
        self.results = results
        self.constraints = constraints

    def summary(self, warnings: Optional[List[str]] = None,
                unresolved_causes: Optional[dict] = None) -> AnalysisSummary:
        class_counts = Counter(f.fault_class.value for f in self.faults)
        subtype_counts = Counter(
            (f.raw_class_token or f.fault_class.value) for f in self.faults
        )
        coverage_loss = [r for r in self.results]

        root_causes = Counter(r.root_cause.value for r in coverage_loss)
        # Rank hotspots over the ACTIONABLE population only. Ranking over the
        # raw loss total puts tie cells at the top -- one constant driver can
        # hold tens of thousands of undetectable faults -- and buries the
        # instances that are actually worth opening. The tied and unmapped
        # populations are enumerated in their own section instead.
        actionable_results = [
            r for r in coverage_loss
            if r.connectivity_known and r.root_cause is not RootCause.TIED_CONSTANT
        ]
        ranked = actionable_results or coverage_loss
        instances = Counter(
            r.instance_name for r in ranked if r.instance_name
        )
        modules = Counter(
            r.mapping.cell_type for r in ranked if r.mapping.cell_type
        )
        constraint_counter: Counter = Counter()
        for r in coverage_loss:
            if r.constraint_related:
                for fact in r.observed_facts:
                    if fact.startswith("Constraint ("):
                        constraint_counter[fact] += 1

        # Evidence quality. Faults that never mapped and faults held at a hard
        # constant are both counted separately: the first rest on no evidence
        # at all, the second are undetectable by construction. Leaving either
        # inside the general loss total is what makes a priority ranking wrong.
        mapped = sum(1 for r in coverage_loss if r.connectivity_known)
        unmapped = len(coverage_loss) - mapped
        scan_counts = Counter(r.scan_cell_state or "unknown"
                              for r in coverage_loss)
        tied = sum(1 for r in coverage_loss
                   if r.root_cause is RootCause.TIED_CONSTANT)
        actionable = len(actionable_results)

        return AnalysisSummary(
            total_faults=len(self.faults),
            class_counts=dict(class_counts),
            subtype_counts=dict(subtype_counts),
            coverage_loss_count=len(coverage_loss),
            top_root_causes=root_causes.most_common(5),
            top_instances=instances.most_common(10),
            top_modules=modules.most_common(10),
            top_constraints=constraint_counter.most_common(5),
            warnings=warnings or [],
            mapped_count=mapped,
            unmapped_count=unmapped,
            scan_evidence_counts=dict(scan_counts),
            tied_constant_count=tied,
            actionable_loss_count=actionable,
            unresolved_causes=dict(unresolved_causes or {}),
        )

    def patterns(self) -> List[PatternGroup]:
        """Group repeated issues to surface systemic problems."""
        groups: List[PatternGroup] = []

        # By root cause.
        rc_counter: Counter = Counter()
        rc_samples = {}
        for r in self.results:
            key = r.root_cause.value
            rc_counter[key] += 1
            rc_samples.setdefault(key, []).append(r.fault.fault_object)
        for key, count in rc_counter.most_common():
            if count >= 2:
                groups.append(PatternGroup(
                    kind="root_cause", key=key, count=count,
                    sample_faults=rc_samples[key][:5],
                ))

        # By instance.
        inst_counter: Counter = Counter()
        inst_samples = {}
        for r in self.results:
            if r.instance_name:
                inst_counter[r.instance_name] += 1
                inst_samples.setdefault(r.instance_name, []).append(
                    r.fault.fault_object)
        for key, count in inst_counter.most_common():
            if count >= 2:
                groups.append(PatternGroup(
                    kind="instance", key=key, count=count,
                    sample_faults=inst_samples[key][:5],
                ))

        # By constraint.
        con_counter: Counter = Counter()
        con_samples = {}
        for r in self.results:
            if r.constraint_related:
                for fact in r.observed_facts:
                    if fact.startswith("Constraint ("):
                        con_counter[fact] += 1
                        con_samples.setdefault(fact, []).append(
                            r.fault.fault_object)
        for key, count in con_counter.most_common():
            if count >= 2:
                groups.append(PatternGroup(
                    kind="constraint", key=key, count=count,
                    sample_faults=con_samples[key][:5],
                ))

        # By unresolved boundary.
        unresolved = [r for r in self.results
                      if r.mapping.confidence.value == "unresolved"]
        if len(unresolved) >= 2:
            groups.append(PatternGroup(
                kind="boundary", key="unresolved_mapping",
                count=len(unresolved),
                sample_faults=[r.fault.fault_object for r in unresolved[:5]],
            ))
        return groups


def build_report(netlist: VerilogNetlist, faults: List[FaultRecord],
                 constraints: List[ConstraintRecord],
                 parser_warnings: Optional[List[str]] = None,
                 progress=None) -> AnalysisReport:
    """Run the full analysis pipeline and return an :class:`AnalysisReport`.

    Args:
        netlist: Parsed netlist.
        faults: Parsed fault records.
        constraints: Parsed constraint records (may be empty).
        parser_warnings: Aggregated warnings from the parsing stage.
        progress: Optional ``callable(done:int, total:int, msg:str)`` for UI
            progress reporting.

    Returns:
        A populated :class:`AnalysisReport`.
    """
    warnings: List[str] = list(parser_warnings or [])
    warnings.extend(netlist.warnings)

    if not constraints:
        warnings.append(
            "No constraints provided/parsed; constraint-related diagnoses are "
            "disabled."
        )

    connectivity = ConnectivityModel(netlist)
    mapper = FaultMapper(connectivity)
    engine = RootCauseEngine(connectivity, mapper, constraints)

    loss_faults = [f for f in faults if f.is_coverage_loss]
    results: List[FaultAnalysisResult] = []
    total = len(loss_faults)
    for idx, fault in enumerate(loss_faults, start=1):
        results.append(engine.analyze_fault(fault))
        if progress is not None and (idx % 25 == 0 or idx == total):
            progress(idx, total, f"Analysed {idx}/{total} coverage-loss faults")

    summarizer = Summarizer(faults, results, constraints)

    # Explain the mapping holes before anything is concluded from them.
    diagnosis = diagnose_unresolved(results, netlist)
    if diagnosis.unresolved:
        warnings.append(diagnosis.note)
        for group in diagnosis.groups[:5]:
            warnings.append(
                f"  unresolved [{group.cause}] x{group.count} "
                f"(family '{group.family}'): "
                f"{group.as_dict()['meaning']}"
            )

    summary = summarizer.summary(warnings, diagnosis.by_cause)
    patterns = summarizer.patterns()

    statistics = compute_statistics(faults)
    selected = enrich_categories(select_categories(statistics), faults)
    attribute_categories(selected, results, connectivity, constraints)
    profile_categories(selected, results, connectivity, constraints)
    recommendations = build_recommendations(statistics, selected)

    logger.info(
        "Analysis complete: %d total faults, %d coverage-loss faults, "
        "%d pattern group(s).",
        len(faults), len(results), len(patterns),
    )
    return AnalysisReport(
        summary=summary,
        fault_results=results,
        pattern_groups=patterns,
        warnings=warnings,
        statistics=statistics,
        selected_categories=selected,
        recommendations=recommendations,
        unresolved_diagnosis=diagnosis,
    )
