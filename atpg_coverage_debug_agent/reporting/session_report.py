"""Save / load a full :class:`AnalysisReport` as JSON.

This lets the GUI persist a completed analysis and reload it later — so a user
can keep working on a report (tables, filtering, and the AI agent) without
re-running ``Analyze``. The serialisation is lossless for everything the UI and
the agent need: summary, per-fault results, repeated patterns, warnings, and
constraints. A compact instance adjacency map is stored too so the agent's
``trace_path`` tool still works after a reload (when the live netlist object is
no longer available).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from ..analysis import investigate
from ..analysis.disposition import DispositionState
from ..analysis.recommend import build_recommendations
from ..analysis.statistics import (
    DerivedStatistics,
    enrich_categories,
    exclude_subclass,
    select_categories,
)
from ..models import (
    AnalysisReport,
    AnalysisSummary,
    ConstraintRecord,
    FaultAnalysisResult,
    FaultClass,
    FaultRecord,
    MappingConfidence,
    MappingResult,
    PatternGroup,
    RootCause,
)

#: Marker + version written at the top of every saved report.
FORMAT_MARKER = "atpg_coverage_debug_report"
FORMAT_VERSION = 1

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def _fault_record_to_dict(f: FaultRecord) -> Dict[str, Any]:
    return {
        "raw_text": f.raw_text,
        "line_number": f.line_number,
        "fault_object": f.fault_object,
        "normalized_object": f.normalized_object,
        "fault_class": f.fault_class.value,
        "raw_class_token": f.raw_class_token,
        "fault_type": f.fault_type,
    }


def _mapping_to_dict(m: MappingResult) -> Dict[str, Any]:
    return {
        "fault_object": m.fault_object,
        "normalized_object": m.normalized_object,
        "confidence": m.confidence.value,
        "instance_name": m.instance_name,
        "cell_type": m.cell_type,
        "matched_net": m.matched_net,
        "candidates": list(m.candidates or []),
        "evidence": list(m.evidence or []),
    }


def _fault_result_to_dict(r: FaultAnalysisResult) -> Dict[str, Any]:
    return {
        "fault": _fault_record_to_dict(r.fault),
        "mapping": _mapping_to_dict(r.mapping),
        "fan_in": list(r.fan_in),
        "fan_out": list(r.fan_out),
        "controllability_issue": r.controllability_issue,
        "observability_issue": r.observability_issue,
        "constraint_related": r.constraint_related,
        "scan_boundary_involved": r.scan_boundary_involved,
        "scan_cell_state": r.scan_cell_state,
        "scan_evidence": r.scan_evidence,
        "tie_driver": dict(r.tie_driver) if r.tie_driver else None,
        "root_cause": r.root_cause.value,
        "observed_facts": list(r.observed_facts or []),
        "inferred_conclusions": list(r.inferred_conclusions or []),
        "evidence": list(r.evidence or []),
        "recommended_step": r.recommended_step,
    }


def _constraint_to_dict(c: ConstraintRecord) -> Dict[str, Any]:
    return {
        "raw_text": c.raw_text,
        "line_number": c.line_number,
        "kind": c.kind,
        "signal": c.signal,
        "normalized_signal": c.normalized_signal,
        "value": c.value,
        "notes": c.notes,
        "directive": c.directive,
        "options": dict(c.options or {}),
        "targets": list(c.targets or []),
        "resolved": c.resolved,
        "source_file": c.source_file,
        "conditional": c.conditional,
    }


def _summary_to_dict(s: AnalysisSummary) -> Dict[str, Any]:
    return {
        "total_faults": s.total_faults,
        "class_counts": dict(s.class_counts),
        "subtype_counts": dict(s.subtype_counts),
        "coverage_loss_count": s.coverage_loss_count,
        "top_root_causes": [list(t) for t in s.top_root_causes],
        "top_instances": [list(t) for t in s.top_instances],
        "top_modules": [list(t) for t in s.top_modules],
        "top_constraints": [list(t) for t in s.top_constraints],
        "warnings": list(s.warnings),
        "mapped_count": s.mapped_count,
        "unmapped_count": s.unmapped_count,
        "scan_evidence_counts": dict(s.scan_evidence_counts),
        "tied_constant_count": s.tied_constant_count,
        "actionable_loss_count": s.actionable_loss_count,
        "unresolved_causes": dict(s.unresolved_causes),
    }


def report_to_dict(report: AnalysisReport) -> Dict[str, Any]:
    """Serialise an :class:`AnalysisReport` into a JSON-ready dict."""
    adjacency = getattr(report, "adjacency", None)
    if not adjacency and report.netlist is not None:
        adjacency = investigate.build_adjacency(report.netlist)
    return {
        "_marker": FORMAT_MARKER,
        "_version": FORMAT_VERSION,
        "summary": _summary_to_dict(report.summary),
        "fault_results": [_fault_result_to_dict(r) for r in report.fault_results],
        "pattern_groups": [
            {"kind": g.kind, "key": g.key, "count": g.count,
             "sample_faults": list(g.sample_faults)}
            for g in report.pattern_groups
        ],
        "warnings": list(report.warnings),
        "constraints": [_constraint_to_dict(c) for c in (report.constraints or [])],
        "adjacency": adjacency or {},
        "sources": getattr(report, "sources", None) or {},
        "investigation": getattr(report, "investigation", None),
        "edits": getattr(report, "edits", None),
        # Only the aggregated breakdown is stored: the selected categories and
        # fix proposals are derived from it deterministically on load.
        "statistics": (report.statistics.as_dict()
                       if getattr(report, "statistics", None) else None),
        "disposition": (report.disposition.as_dict()
                        if getattr(report, "disposition", None) else None),
        # A manifest of the per-category fault files written beside this
        # session. The names are relative to the session file, so they only
        # resolve from the folder it was saved in.
        "category_dumps": [d.as_dict()
                           for d in (getattr(report, "category_dumps", None)
                                     or [])],
        # How to reopen this design in the vendor viewer. Only the form the
        # user filled in; the generated scripts are scratch and regenerate.
        "visualizer_config": (dict(report.visualizer_config)
                              if getattr(report, "visualizer_config", None)
                              else None),
        # What the inputs declared and how well they were understood. These
        # travel with the session because a reloaded report must be able to
        # say whether its census is collapsed, which classes it could not
        # interpret, and how much of the constraint file it actually parsed.
        "fault_list_header": (report.fault_list_header.as_dict()
                              if getattr(report, "fault_list_header", None)
                              else None),
        "class_diagnostics": (report.class_diagnostics.as_dict()
                              if getattr(report, "class_diagnostics", None)
                              else None),
        "constraint_diagnostics": getattr(report, "constraint_diagnostics",
                                          None),
        "analysis_config": getattr(report, "analysis_config", None),
    }


def save_report(report: AnalysisReport, path: str,
                dump_categories: bool = True) -> None:
    """Write *report* to *path* as JSON.

    Args:
        report: The analysed report.
        path: Destination of the session JSON.
        dump_categories: Also write one CSV and one JSON per selected
            coverage-loss category into a sidecar folder beside *path*, so the
            saved session carries the faults behind each category and the
            whole folder can be handed to someone else. Set False to write the
            session JSON alone.
    """
    if dump_categories:
        from .category_dump import write_category_dumps

        try:
            write_category_dumps(report, path)
        except OSError as exc:
            # A read-only destination must not cost the user their session.
            logger.warning("Category dumps not written next to %s: %s",
                           path, exc)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report_to_dict(report), fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Deserialisation
# ---------------------------------------------------------------------------
def _dict_to_fault_record(d: Dict[str, Any]) -> FaultRecord:
    return FaultRecord(
        raw_text=d.get("raw_text", ""),
        line_number=int(d.get("line_number", 0) or 0),
        fault_object=d.get("fault_object", ""),
        normalized_object=d.get("normalized_object", d.get("fault_object", "")),
        fault_class=FaultClass(d.get("fault_class", "UNKNOWN")),
        raw_class_token=d.get("raw_class_token", ""),
        fault_type=d.get("fault_type"),
    )


def _dict_to_mapping(d: Dict[str, Any]) -> MappingResult:
    return MappingResult(
        fault_object=d.get("fault_object", ""),
        normalized_object=d.get("normalized_object", ""),
        confidence=MappingConfidence(d.get("confidence", "unresolved")),
        instance_name=d.get("instance_name"),
        cell_type=d.get("cell_type"),
        matched_net=d.get("matched_net"),
        candidates=list(d.get("candidates", [])),
        evidence=list(d.get("evidence", [])),
    )


def _dict_to_fault_result(d: Dict[str, Any]) -> FaultAnalysisResult:
    return FaultAnalysisResult(
        fault=_dict_to_fault_record(d.get("fault", {})),
        mapping=_dict_to_mapping(d.get("mapping", {})),
        fan_in=list(d.get("fan_in", [])),
        fan_out=list(d.get("fan_out", [])),
        controllability_issue=bool(d.get("controllability_issue", False)),
        observability_issue=bool(d.get("observability_issue", False)),
        constraint_related=bool(d.get("constraint_related", False)),
        scan_boundary_involved=bool(d.get("scan_boundary_involved", False)),
        scan_cell_state=d.get("scan_cell_state", "unknown"),
        scan_evidence=d.get("scan_evidence", ""),
        tie_driver=d.get("tie_driver") or None,
        root_cause=RootCause(d.get("root_cause",
                                   RootCause.OTHER_STRUCTURAL.value)),
        observed_facts=list(d.get("observed_facts", [])),
        inferred_conclusions=list(d.get("inferred_conclusions", [])),
        evidence=list(d.get("evidence", [])),
        recommended_step=d.get("recommended_step", ""),
    )


def _dict_to_constraint(d: Dict[str, Any]) -> ConstraintRecord:
    return ConstraintRecord(
        raw_text=d.get("raw_text", ""),
        line_number=int(d.get("line_number", 0) or 0),
        kind=d.get("kind", ""),
        signal=d.get("signal"),
        normalized_signal=d.get("normalized_signal"),
        value=d.get("value"),
        notes=d.get("notes", ""),
        directive=d.get("directive", ""),
        options=dict(d.get("options") or {}),
        targets=list(d.get("targets") or []),
        resolved=bool(d.get("resolved", True)),
        source_file=d.get("source_file", ""),
        conditional=bool(d.get("conditional", False)),
    )


def _dict_to_summary(d: Dict[str, Any]) -> AnalysisSummary:
    def _tuples(key: str) -> List[tuple]:
        return [tuple(t) for t in d.get(key, [])]

    return AnalysisSummary(
        total_faults=int(d.get("total_faults", 0) or 0),
        class_counts=dict(d.get("class_counts", {})),
        subtype_counts=dict(d.get("subtype_counts", {})),
        coverage_loss_count=int(d.get("coverage_loss_count", 0) or 0),
        top_root_causes=_tuples("top_root_causes"),
        top_instances=_tuples("top_instances"),
        top_modules=_tuples("top_modules"),
        top_constraints=_tuples("top_constraints"),
        warnings=list(d.get("warnings", [])),
        mapped_count=int(d.get("mapped_count", 0) or 0),
        unmapped_count=int(d.get("unmapped_count", 0) or 0),
        scan_evidence_counts=dict(d.get("scan_evidence_counts", {})),
        tied_constant_count=int(d.get("tied_constant_count", 0) or 0),
        actionable_loss_count=int(d.get("actionable_loss_count", 0) or 0),
        unresolved_causes=dict(d.get("unresolved_causes", {})),
    )


def dict_to_report(data: Dict[str, Any]) -> AnalysisReport:
    """Rebuild an :class:`AnalysisReport` from a :func:`report_to_dict` dict."""
    if data.get("_marker") != FORMAT_MARKER:
        raise ValueError(
            "This file is not an ATPG coverage-debug report (missing marker).")
    report = AnalysisReport(
        summary=_dict_to_summary(data.get("summary", {})),
        fault_results=[_dict_to_fault_result(r)
                       for r in data.get("fault_results", [])],
        pattern_groups=[
            PatternGroup(kind=g.get("kind", ""), key=g.get("key", ""),
                         count=int(g.get("count", 0) or 0),
                         sample_faults=list(g.get("sample_faults", [])))
            for g in data.get("pattern_groups", [])
        ],
        warnings=list(data.get("warnings", [])),
    )
    report.constraints = [_dict_to_constraint(c)
                          for c in data.get("constraints", [])]
    report.faults = [r.fault for r in report.fault_results]
    report.netlist = None
    report.adjacency = data.get("adjacency", {})
    report.sources = data.get("sources", {}) or {}
    report.investigation = data.get("investigation")
    report.edits = data.get("edits")

    from ..diagnostics import UnrecognisedReport, UnrecognisedToken
    from ..parser.fault_parser import FaultListHeader

    report.fault_list_header = FaultListHeader.from_dict(
        data.get("fault_list_header"))
    diag = data.get("class_diagnostics")
    if diag:
        report.class_diagnostics = UnrecognisedReport(
            domain=str(diag.get("domain", "fault class")),
            total=int(diag.get("total", 0) or 0),
            threshold_pct=float(diag.get("threshold_pct", 0.0) or 0.0),
            fatal=bool(diag.get("fatal", False)),
            tokens=[UnrecognisedToken(
                token=str(t.get("token", "")),
                count=int(t.get("count", 0) or 0),
                samples=list(t.get("samples") or []),
                first_line=t.get("first_line"),
            ) for t in (diag.get("tokens") or [])],
        )
    report.constraint_diagnostics = data.get("constraint_diagnostics")
    report.analysis_config = data.get("analysis_config")

    stats_payload = data.get("statistics")
    if stats_payload:
        statistics = DerivedStatistics.from_dict(stats_payload)
        report.statistics = statistics
        report.disposition = DispositionState.from_dict(
            data.get("disposition"))

        # The relevant population is derived, not stored: it is the total
        # minus one subclass, so recomputing it cannot disagree with the
        # census the way a second saved copy could drift from it.
        triage_stats = statistics
        triage_faults = report.faults or []
        if report.disposition is not None \
                and report.disposition.has_relevant_population:
            report.relevant_statistics = exclude_subclass(
                statistics, report.disposition.waiver_subclass)
            waived = report.disposition.waiver_subclass.upper()
            triage_stats = report.relevant_statistics
            triage_faults = [f for f in triage_faults
                             if (getattr(f, "dotted_class", "") or "").upper()
                             != waived]

        # Clustering is rebuilt from the coverage-loss faults that were saved.
        # Detected faults are not persisted, so clusters cover the loss
        # population only — which is all the triage looks at anyway.
        report.selected_categories = enrich_categories(
            select_categories(triage_stats), triage_faults)
        report.recommendations = build_recommendations(
            triage_stats, report.selected_categories)

    dumps_payload = data.get("category_dumps") or []
    if dumps_payload:
        from .category_dump import CategoryDump

        report.category_dumps = [CategoryDump.from_dict(d)
                                 for d in dumps_payload]

    viewer = data.get("visualizer_config")
    if viewer:
        report.visualizer_config = dict(viewer)
    return report


def load_report(path: str) -> AnalysisReport:
    """Load and rebuild an :class:`AnalysisReport` from JSON at *path*."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return dict_to_report(data)
