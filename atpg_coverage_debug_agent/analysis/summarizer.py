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
)
from ..parser.verilog_parser import VerilogNetlist
from .connectivity import ConnectivityModel
from .mapper import FaultMapper
from .root_cause import RootCauseEngine

logger = logging.getLogger(__name__)


class Summarizer:
    """Builds the executive summary and repeated-pattern groups."""

    def __init__(self, faults: List[FaultRecord],
                 results: List[FaultAnalysisResult],
                 constraints: List[ConstraintRecord]) -> None:
        self.faults = faults
        self.results = results
        self.constraints = constraints

    def summary(self, warnings: Optional[List[str]] = None) -> AnalysisSummary:
        class_counts = Counter(f.fault_class.value for f in self.faults)
        subtype_counts = Counter(
            (f.raw_class_token or f.fault_class.value) for f in self.faults
        )
        coverage_loss = [r for r in self.results]

        root_causes = Counter(r.root_cause.value for r in coverage_loss)
        instances = Counter(
            r.instance_name for r in coverage_loss if r.instance_name
        )
        modules = Counter(
            r.mapping.cell_type for r in coverage_loss if r.mapping.cell_type
        )
        constraint_counter: Counter = Counter()
        for r in coverage_loss:
            if r.constraint_related:
                for fact in r.observed_facts:
                    if fact.startswith("Constraint ("):
                        constraint_counter[fact] += 1

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
    summary = summarizer.summary(warnings)
    patterns = summarizer.patterns()

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
    )
