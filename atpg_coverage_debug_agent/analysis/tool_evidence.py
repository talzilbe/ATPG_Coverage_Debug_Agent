"""Fold the ATPG tool's own measurements into the offline analysis.

Every number this analyzer produces about *why* a fault is lost is an estimate
from structure. When the tool's ``report_statistics`` or ``analyze_fault``
output is supplied, it is the measurement, and this module makes it win:

* coverage metrics and class counts are put side by side with the computed
  ones, with the delta, so a disagreement is visible rather than averaged;
* named tie sources the tool lists are checked against the traced ones;
* ``analyze_fault`` samples are classified with the same decision rules the
  structural profile uses, and where a category has samples the *measured*
  dominant verdict replaces the estimated one when choosing fixes.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import EvidenceSource
from ..parser.tessent_reports import (
    FaultAnalysis, StatisticsReport, read_tool_reports,
)
from .reachability import (
    OBS_BOTTLENECK, RECONVERGENCE_HIGH, SEQ_DEPTH_HIGH, SIGNATURES,
)

logger = logging.getLogger(__name__)

#: A computed metric within this many percentage points of the tool's agrees.
METRIC_TOLERANCE_PCT = 0.05

#: Basis note: the two sides may legitimately count different populations.
BASIS_NOTE = (
    "Tessent reports collapsed faults unless asked otherwise, while this "
    "analyzer counts the fault list as written (uncollapsed). A small, "
    "consistent delta is a basis difference; a large or class-specific one is "
    "worth explaining. Where they differ, the tool's figure is authoritative.")


def measured_verdict(entry: FaultAnalysis) -> str:
    """The structural verdict an ``analyze_fault`` entry implies.

    Same precedence as :func:`.reachability.classify_site`, applied to the
    tool's measured fields instead of netlist estimates.
    """
    if entry.observe_depth is not None and entry.observe_depth >= SEQ_DEPTH_HIGH:
        return "sequential_depth_explosion"
    if entry.activatable is False:
        return "low_controllability"
    if entry.observation_points is None:
        return "undetermined"
    if entry.observation_points == 0:
        return "hard_observability_gap"
    if entry.observation_points <= OBS_BOTTLENECK:
        return "observability_bottleneck"
    if entry.observation_points >= RECONVERGENCE_HIGH:
        return "reconvergent_complexity"
    return "no_structural_blocker"


def _norm(path: str) -> str:
    return (path or "").strip().lstrip("/").replace(".", "/").lower()


@dataclass
class ToolEvidence:
    """What the tool reported, and where it agrees with this analysis."""

    source: str = ""
    statistics: List[StatisticsReport] = field(default_factory=list)
    analyses: List[FaultAnalysis] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metric_check: List[Dict[str, Any]] = field(default_factory=list)
    class_check: List[Dict[str, Any]] = field(default_factory=list)
    tie_check: List[Dict[str, Any]] = field(default_factory=list)
    site_check: List[Dict[str, Any]] = field(default_factory=list)
    #: subclass -> {"samples", "verdicts", "dominant", "share"}.
    measured_categories: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def disagreements(self) -> int:
        rows = self.metric_check + self.class_check + self.site_check
        return sum(1 for r in rows if r.get("agree") is False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "statistics": [s.as_dict() for s in self.statistics],
            "analyses": [a.as_dict() for a in self.analyses],
            "warnings": list(self.warnings),
            "metric_check": list(self.metric_check),
            "class_check": list(self.class_check),
            "tie_check": list(self.tie_check),
            "site_check": list(self.site_check),
            "measured_categories": dict(self.measured_categories),
            "basis_note": BASIS_NOTE,
            "evidence_source": EvidenceSource.TOOL_REPORT.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolEvidence":
        return cls(
            source=str(data.get("source", "")),
            statistics=[StatisticsReport.from_dict(s)
                        for s in data.get("statistics") or []],
            analyses=[FaultAnalysis.from_dict(a)
                      for a in data.get("analyses") or []],
            warnings=list(data.get("warnings") or []),
        )


def load_tool_evidence(path: str) -> ToolEvidence:
    stats, analyses, warnings = read_tool_reports(path)
    return ToolEvidence(source=path, statistics=stats, analyses=analyses,
                        warnings=warnings)


def _metric_rows(measured: StatisticsReport, report: Any) -> List[Dict[str, Any]]:
    stats = getattr(report, "statistics", None)
    computed = stats.metrics() if stats is not None else {}
    rows = []
    for name, value in measured.metrics.items():
        ours = computed.get(name)
        delta = None if ours is None else round(ours - value, 4)
        rows.append({
            "metric": name, "measured": value, "computed": ours,
            "delta": delta,
            "agree": None if delta is None else abs(delta) <= METRIC_TOLERANCE_PCT,
        })
    return rows


def _class_rows(measured: StatisticsReport, report: Any) -> List[Dict[str, Any]]:
    stats = getattr(report, "statistics", None)
    if stats is None:
        return []
    ours: Dict[str, int] = {}
    for s in stats.subclass_stats:
        family = (s.family or s.subclass_id.split(".")[0]).upper()
        ours[family] = ours.get(family, 0) + s.count
        if "." in s.subclass_id:
            ours[s.subclass_id.upper()] = s.count
    rows = []
    for key, value in list(measured.classes.items()) + list(
            measured.subclasses.items()):
        if key in ("FU",):
            continue
        mine = ours.get(key.upper())
        rows.append({
            "class": key, "measured": value, "computed": mine,
            "delta": None if mine is None else mine - value,
            "agree": None if mine is None else mine == value,
        })
    return rows


def _tie_rows(measured: StatisticsReport, report: Any) -> List[Dict[str, Any]]:
    traced: Dict[str, int] = {}
    for category in getattr(report, "selected_categories", None) or []:
        if category.subclass_id != "AU.TC":
            continue
        attribution = getattr(category, "attribution", None)
        for source in getattr(attribution, "tie_sources", None) or []:
            traced[_norm(source.driver)] = traced.get(_norm(source.driver), 0) \
                + int(source.count)
    rows = []
    for tie in measured.tie_sources:
        key = _norm(tie.path)
        match = next((name for name in traced
                      if key.endswith(name) or name.endswith(key)), None)
        rows.append({
            "path": tie.path, "value": tie.value, "measured": tie.count,
            "traced": traced.get(match) if match else None,
            "found_by_tracing": match is not None,
        })
    return rows


def _site_rows(evidence: ToolEvidence, report: Any) -> List[Dict[str, Any]]:
    by_object: Dict[str, Any] = {}
    by_instance: Dict[str, Any] = {}
    for fr in getattr(report, "fault_results", None) or []:
        obj = _norm(fr.fault.fault_object)
        by_object.setdefault(obj, fr)
        by_instance.setdefault(obj.rsplit("/", 1)[0], fr)
    estimated: Dict[str, str] = {}
    for category in getattr(report, "selected_categories", None) or []:
        reach = getattr(category, "reachability", None)
        if reach is not None and getattr(reach, "dominant", ""):
            estimated[category.subclass_id] = reach.estimated_dominant \
                if getattr(reach, "estimated_dominant", "") else reach.dominant
    rows = []
    for entry in evidence.analyses:
        fr = (by_object.get(_norm(entry.fault_object))
              or by_instance.get(_norm(entry.instance)))
        subclass = (fr.fault.dotted_class if fr is not None
                    else entry.fault_class) or entry.fault_class
        verdict = measured_verdict(entry)
        ours = estimated.get(subclass)
        rows.append({
            "fault_object": entry.fault_object,
            "subclass": subclass,
            "in_fault_list": fr is not None,
            "measured_verdict": verdict,
            "estimated_verdict": ours,
            "agree": (None if ours is None or verdict == "undetermined"
                      else ours == verdict),
            "observation_points": entry.observation_points,
            "activatable": entry.activatable,
            "observe_depth": entry.observe_depth,
            "status": entry.status,
        })
    return rows


def _measured_categories(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Counter] = {}
    for row in rows:
        if row["measured_verdict"] == "undetermined" or not row["subclass"]:
            continue
        grouped.setdefault(row["subclass"], Counter())[row["measured_verdict"]] += 1
    out = {}
    for subclass, counts in grouped.items():
        dominant, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        total = sum(counts.values())
        out[subclass] = {"samples": total, "verdicts": dict(counts),
                         "dominant": dominant, "share": round(n / total, 4)}
    return out


def _override_category(category: Any, measured: Dict[str, Any]) -> None:
    """Make the measured verdict drive the fix choice for *category*."""
    reach = getattr(category, "reachability", None)
    entry = SIGNATURES.get(measured["dominant"], {})
    if reach is None:
        return
    if not getattr(reach, "estimated_dominant", ""):
        reach.estimated_dominant = reach.dominant
    reach.measured = dict(measured)
    reach.preferred_fix_ids = list(entry.get("fix_ids", []))
    estimated = SIGNATURES.get(reach.estimated_dominant, {}).get(
        "label", reach.estimated_dominant)
    verb = ("confirms" if reach.estimated_dominant == measured["dominant"]
            else f"overrides the structural estimate ({estimated})")
    prefix = (f"[measured by analyze_fault on {measured['samples']} sample(s)] "
              f"{entry.get('label', measured['dominant'])} in "
              f"{measured['share']:.0%} of samples; this {verb}. ")
    base = getattr(reach, "_note_before_measurement", None)
    if base is None:
        reach._note_before_measurement = reach.note
        base = reach.note
    reach.note = prefix + base


def apply_tool_evidence(report: Any, evidence: Optional[ToolEvidence]) -> Any:
    """Cross-check *report* against *evidence* and let measurements win.

    Recomputes the checks from scratch, so it is safe to call again after the
    triage was rebuilt (a reload, a waiver).
    """
    if evidence is None:
        return report
    evidence.metric_check, evidence.class_check, evidence.tie_check = [], [], []
    for measured in evidence.statistics:
        evidence.metric_check.extend(_metric_rows(measured, report))
        evidence.class_check.extend(_class_rows(measured, report))
        evidence.tie_check.extend(_tie_rows(measured, report))
    evidence.site_check = _site_rows(evidence, report)
    evidence.measured_categories = _measured_categories(evidence.site_check)

    changed = False
    for category in getattr(report, "selected_categories", None) or []:
        measured = evidence.measured_categories.get(category.subclass_id)
        if measured and getattr(category, "reachability", None) is not None:
            _override_category(category, measured)
            changed = True
    if changed:
        from .recommend import build_recommendations
        stats = (getattr(report, "relevant_statistics", None)
                 or getattr(report, "statistics", None))
        if stats is not None:
            report.recommendations = build_recommendations(
                stats, report.selected_categories)

    report.tool_evidence = evidence
    notes = list(evidence.warnings)
    if evidence.disagreements:
        notes.append(
            f"Tool reports disagree with this analysis in "
            f"{evidence.disagreements} place(s); the tool's figures are "
            f"authoritative (Triage tab, 'Measured by Tessent').")
    for note in notes:
        if note not in report.warnings:
            report.warnings.append(note)
    if getattr(report, "summary", None) is not None:
        report.summary.warnings = list(report.warnings)
    return report


def measured_metrics(report: Any) -> Dict[str, float]:
    """The tool's own coverage metrics, when a report_statistics was read."""
    evidence = getattr(report, "tool_evidence", None)
    for measured in getattr(evidence, "statistics", None) or []:
        if measured.metrics:
            return dict(measured.metrics)
    return {}


# ---------------------------------------------------------------------------
# Collecting the evidence from a live session
# ---------------------------------------------------------------------------
#: analyze_fault samples per category by default, and the hard cap.
DEFAULT_SAMPLES = 5
MAX_SAMPLES = 20

#: Categories a structural verdict exists for, so a sample can confirm it.
_SAMPLED_FAMILIES = ("UC.AAB", "UO.AAB", "UC", "UO", "AU.SEQ", "AU.TC",
                     "AU.PC", "AU.BB")


def _confidence_text(fr: Any) -> str:
    value = getattr(getattr(fr, "mapping", None), "confidence", "")
    return str(getattr(value, "value", value) or "").lower()


def collect_targets(fault_results: Any, subclasses: Optional[List[str]] = None,
                    samples: int = DEFAULT_SAMPLES,
                    triage: Optional[Dict[str, Any]] = None,
                    ) -> Dict[str, List[Any]]:
    """Pick ``(fault path, stuck value)`` samples per category, spread out.

    Defaults to the triage's selected categories that carry a structural
    verdict. Mapped faults with a recorded stuck-at value are preferred, and
    the pick is strided across the list so one hierarchy does not dominate.
    """
    samples = max(1, min(int(samples or DEFAULT_SAMPLES), MAX_SAMPLES))
    wanted = [s.strip() for s in (subclasses or []) if s and s.strip()]
    if not wanted:
        selected = [str(c.get("subclass", "")) for c in
                    (triage or {}).get("selected", []) or []]
        wanted = [s for s in selected if s in _SAMPLED_FAMILIES] or selected
    grouped: Dict[str, List[Any]] = {s: [] for s in wanted}
    for fr in fault_results or ():
        fault = getattr(fr, "fault", None)
        dotted = str(getattr(fault, "dotted_class", "") or "")
        if dotted in grouped and str(getattr(fault, "fault_type", "")) in ("0", "1"):
            grouped[dotted].append(fr)
    out: Dict[str, List[Any]] = {}
    for subclass, rows in grouped.items():
        mapped = [r for r in rows if _confidence_text(r) != "unresolved"]
        pool = mapped or rows
        if not pool:
            continue
        step = max(1, len(pool) // samples)
        out[subclass] = [(r.fault.fault_object, str(r.fault.fault_type))
                         for r in pool[::step][:samples]]
    return out


def build_collect_script(targets: Dict[str, List[Any]],
                         statistics: bool = True) -> str:
    """Tcl that prints report_statistics and one analyze_fault per sample.

    Each analyze_fault is wrapped in ``catch`` so one unknown path does not
    stop the rest; the tool's own output still reaches the transcript.
    """
    lines = ["# Collected by the ATPG Coverage Debug Agent. Read-only."]
    if statistics:
        lines.append("report_statistics -detailed_analysis")
    for subclass, picks in targets.items():
        lines.append(f"# {subclass}: {len(picks)} sample(s)")
        for path, stuck in picks:
            if any(c in path for c in "{}\\\n"):
                continue
            lines.append(f"catch {{analyze_fault {{{path}}} -stuck_at {stuck}}}")
    return "\n".join(lines) + "\n"


def evidence_from_transcript(text: str, source: str = "live Tessent session"
                             ) -> ToolEvidence:
    """Parse whatever the session printed into a :class:`ToolEvidence`."""
    from ..parser.tessent_reports import (parse_analyze_fault,
                                          parse_report_statistics)
    evidence = ToolEvidence(source=source)
    stats = parse_report_statistics(text or "", source)
    if not stats.empty:
        evidence.statistics.append(stats)
    evidence.analyses = parse_analyze_fault(text or "")
    if stats.empty and not evidence.analyses:
        evidence.warnings.append(
            "The session printed no report_statistics table and no "
            "analyze_fault entry; nothing was measured.")
    return evidence


def merge_evidence(base: Optional[ToolEvidence],
                   extra: ToolEvidence) -> ToolEvidence:
    """Add *extra* to *base*; a re-analysed fault keeps its newest entry."""
    if base is None:
        return extra
    merged = ToolEvidence(source="; ".join(s for s in (base.source, extra.source)
                                           if s))
    merged.statistics = list(extra.statistics or base.statistics)
    seen = {}
    for entry in list(base.analyses) + list(extra.analyses):
        seen[(_norm(entry.fault_object), entry.stuck)] = entry
    merged.analyses = list(seen.values())
    merged.warnings = list(dict.fromkeys(base.warnings + extra.warnings))
    return merged


def summarize_for_agent(evidence: ToolEvidence,
                        triage: Optional[Dict[str, Any]] = None,
                        context: Optional[Dict[str, Any]] = None,
                        ) -> Dict[str, Any]:
    """What the measurement says, beside the offline estimate, as plain data."""
    estimated = {}
    for c in (triage or {}).get("selected", []) or []:
        reach = c.get("reachability") or {}
        if reach.get("dominant"):
            estimated[c.get("subclass")] = reach["dominant"]
    computed = ((context or {}).get("coverage_metrics") or {})
    per_category: Dict[str, Counter] = {}
    samples = []
    for entry in evidence.analyses:
        verdict = measured_verdict(entry)
        subclass = entry.fault_class or "?"
        per_category.setdefault(subclass, Counter())[verdict] += 1
        samples.append({"fault": entry.fault_object, "stuck": entry.stuck,
                        "class": subclass, "measured_verdict": verdict,
                        "observation_points": entry.observation_points,
                        "activatable": entry.activatable,
                        "observe_depth": entry.observe_depth,
                        "status": entry.status})
    categories = []
    for subclass, counts in per_category.items():
        dominant, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        ours = estimated.get(subclass)
        categories.append({
            "subclass": subclass, "samples": sum(counts.values()),
            "verdicts": dict(counts), "measured_dominant": dominant,
            "estimated_dominant": ours,
            "agrees_with_estimate": None if ours is None else ours == dominant,
            "fixes_for_measured": list(SIGNATURES.get(dominant, {})
                                       .get("fix_ids", [])),
        })
    metrics = []
    for stats in evidence.statistics:
        for name, value in stats.metrics.items():
            ours = computed.get(name)
            metrics.append({"metric": name, "measured": value,
                            "computed": ours,
                            "delta": None if ours is None
                            else round(ours - value, 4)})
    return {
        "measured_categories": categories,
        "coverage_metrics": metrics,
        "classes": (evidence.statistics[0].subclasses
                    if evidence.statistics else {}),
        "tie_sources": ([vars(t) for t in evidence.statistics[0].tie_sources]
                        if evidence.statistics else []),
        "samples": samples,
        "warnings": list(evidence.warnings),
        "basis_note": BASIS_NOTE,
        "evidence_source": EvidenceSource.TOOL_REPORT.value,
    }
