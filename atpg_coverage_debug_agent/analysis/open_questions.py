"""Where the offline analysis is weakest, stated as questions for the reviewer.

The agent's job is to review the offline analysis, and its tool budget is
finite. Left to itself it spends that budget re-checking the conclusions the
analysis is surest of, because those are the ones the payload states most
prominently. This module inverts that: it walks the report for every place
the analysis itself recorded reduced confidence, a partial picture, a mixed
verdict, an unreconciled figure or a contradiction, and hands those over as
an ordered list of questions -- each naming the tool that would settle it.

Everything here is derived from fields the analysis already computed. No new
inference happens, so an open question is never itself a claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import VerdictConfidence
from .disposition import STATE_PRE

#: Share of the coverage loss that failed to map above which mapping itself
#: becomes the first question.
UNMAPPED_SHARE_HIGH = 0.20
#: Attribution coverage below which the blocking picture is called partial
#: (mirrors ``attribution`` module's own wording).
ATTRIBUTION_PARTIAL = 0.5
#: Disagreeing pairs surfaced individually; the rest are summarised.
MAX_AGREEMENT_LEADS = 5

PRIORITY_LABELS = {1: "first", 2: "next", 3: "if time allows"}


@dataclass
class OpenQuestion:
    """One thing the offline analysis could not settle."""

    id: str
    priority: int
    subject: str
    question: str
    why: str
    suggested_tools: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "priority": self.priority,
                "priority_label": PRIORITY_LABELS.get(self.priority, ""),
                "subject": self.subject, "question": self.question,
                "why": self.why, "suggested_tools": list(self.suggested_tools)}


def _conf(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def build_open_questions(report: Any,
                         agreement: Optional[Any] = None) -> List[OpenQuestion]:
    """Collect the open questions for *report*, highest priority first.

    Args:
        report: An ``AnalysisReport``.
        agreement: The subclass/root-cause cross-check, when computed
            separately; ``report.agreement`` is used otherwise.
    """
    questions: List[OpenQuestion] = []
    summary = getattr(report, "summary", None)

    # -- population-level -------------------------------------------------
    try:
        from .census import build_census
        rec = build_census(report).reconciliation()
    except Exception:  # noqa: BLE001 - a census failure is reported elsewhere
        rec = None
    if rec is not None and not rec.get("reconciles", True):
        questions.append(OpenQuestion(
            id="census:unreconciled", priority=1, subject="census",
            question="Why do the fault-class counts not sum to the total?",
            why=(f"Class counts sum to {rec['sum_of_class_counts']} against "
                 f"{rec['total_faults']} faults (delta {rec['delta']}). No "
                 "coverage metric derived from this census is trustworthy."),
            suggested_tools=["report_context", "report_handoff_gap"]))

    state = getattr(report, "disposition", None)
    if state is not None and getattr(state, "state", "") == STATE_PRE:
        questions.append(OpenQuestion(
            id="disposition:pre", priority=1, subject="disposition",
            question=("Does the category ranking survive the fault-"
                      "disposition step?"),
            why=("The fault list predates disposition, which rewrites the "
                 "ATPG-untestable subclass distribution the ranking is built "
                 "from."),
            suggested_tools=["report_context", "coverage_triage"]))

    loss = int(getattr(summary, "coverage_loss_count", 0) or 0)
    unmapped = int(getattr(summary, "unmapped_count", 0) or 0)
    if loss and unmapped / loss > UNMAPPED_SHARE_HIGH:
        questions.append(OpenQuestion(
            id="mapping:unmapped_share", priority=1, subject="mapping",
            question=("Why did "
                      f"{unmapped} of {loss} coverage-loss faults not map "
                      "onto the netlist?"),
            why=("Connectivity is unknown for every unmapped fault, so no "
                 "structural root cause on them is provable. At "
                 f"{100.0 * unmapped / loss:.1f}% of the loss this bounds "
                 "how much of the triage can be trusted."),
            suggested_tools=["diagnose_unresolved", "report_context"]))

    diag = getattr(report, "constraint_diagnostics", None) or {}
    unresolved_directives = int(diag.get("unresolved", 0) or 0) if diag else 0
    if unresolved_directives:
        questions.append(OpenQuestion(
            id="constraints:partially_parsed", priority=3,
            subject="constraints",
            question=("Which constraints hide in the "
                      f"{unresolved_directives} unevaluated directive(s)?"),
            why=("Conditional or unrecognised dofile blocks were not "
                 "evaluated, so 'no constraint affects this fault' is only "
                 "as strong as the part of the file that was parsed."),
            suggested_tools=["list_constraints", "report_context"]))

    # -- per selected category ---------------------------------------------
    for cat in (getattr(report, "selected_categories", None) or []):
        sub = getattr(cat, "subclass_id", "") or ""
        verdict = getattr(cat, "verdict", None)
        if verdict is not None:
            conf = _conf(getattr(verdict, "confidence", ""))
            if conf in (VerdictConfidence.REDUCED.value,
                        VerdictConfidence.INSUFFICIENT.value):
                reason = (getattr(verdict, "reason", "")
                          or "no reason recorded")
                questions.append(OpenQuestion(
                    id=f"category:{sub}:confidence", priority=2, subject=sub,
                    question=f"Is {sub} actually actionable as ranked?",
                    why=(f"The scored verdict carries {conf} confidence "
                         f"({reason})."),
                    suggested_tools=["list_clusters", "list_category_faults",
                                     "recommend_fixes"]))
        attribution = getattr(cat, "attribution", None)
        if attribution is not None:
            analysed = int(getattr(attribution, "analysed", 0) or 0)
            attributed = int(getattr(attribution, "attributed", 0) or 0)
            if analysed and attributed / analysed < ATTRIBUTION_PARTIAL:
                questions.append(OpenQuestion(
                    id=f"category:{sub}:blockers", priority=2, subject=sub,
                    question=f"What blocks the rest of {sub}?",
                    why=(f"Only {attributed} of {analysed} analysed faults "
                         "were traced to a named blocker; the picture is "
                         "partial."),
                    suggested_tools=["list_blocking_sources", "why_blocked",
                                     "get_fault_detail"]))
        reach = getattr(cat, "reachability", None)
        if reach is not None and int(getattr(reach, "profiled", 0) or 0):
            truncated = int(getattr(reach, "truncated_sites", 0) or 0)
            if not getattr(reach, "consensus", False):
                questions.append(OpenQuestion(
                    id=f"category:{sub}:mixed_profile", priority=2,
                    subject=sub,
                    question=(f"Which structural signature really drives "
                              f"{sub}?"),
                    why=(f"The dominant signature "
                         f"'{getattr(reach, 'dominant', '')}' covers only "
                         f"{100.0 * float(getattr(reach, 'dominant_share', 0.0)):.0f}% "
                         "of the profiled sites; the category is structurally "
                         "mixed and a single fix will not cover it."),
                    suggested_tools=["profile_fault_sites", "get_fault_detail",
                                     "trace_path"]))
            if truncated:
                questions.append(OpenQuestion(
                    id=f"category:{sub}:truncated_cones", priority=3,
                    subject=sub,
                    question=(f"Are the {truncated} truncated cone(s) in "
                              f"{sub} observability gaps or merely wide?"),
                    why=("The cone walk hit its node cap, so observation-"
                         "point counts are lower bounds and no observability "
                         "verdict was issued for those sites."),
                    suggested_tools=["profile_fault_sites", "trace_path"]))

    # -- classification cross-check ------------------------------------------
    agr = agreement if agreement is not None else getattr(report, "agreement",
                                                          None)
    leads = list(getattr(agr, "leads", None) or [])
    for lead in leads[:MAX_AGREEMENT_LEADS]:
        contradiction = lead.get("verdict", "disagree") == "disagree"
        if contradiction:
            question = (f"{lead['count']} fault(s) the ATPG tool classes "
                        f"{lead['subclass']} resolve here to "
                        f"'{lead['root_cause']}' -- which side is wrong?")
        else:
            question = (f"What mechanism makes the {lead['count']} "
                        f"{lead['subclass']} fault(s) this tool could not "
                        "explain structurally untestable?")
        questions.append(OpenQuestion(
            id=f"agreement:{lead['subclass']}:{lead['root_cause']}",
            priority=1 if contradiction else 2, subject=lead["subclass"],
            question=question,
            why=lead.get("why_it_matters", ""),
            suggested_tools=list(lead.get("suggested_tools") or [])))
    if len(leads) > MAX_AGREEMENT_LEADS:
        rest = sum(int(x.get("count", 0)) for x in leads[MAX_AGREEMENT_LEADS:])
        questions.append(OpenQuestion(
            id="agreement:more", priority=2, subject="classification",
            question=(f"{len(leads) - MAX_AGREEMENT_LEADS} further "
                      f"disagreeing subclass/root-cause pairs ({rest} "
                      "faults) -- any pattern?"),
            why="Listed in full by the classification_crosscheck tool.",
            suggested_tools=["classification_crosscheck"]))

    questions.sort(key=lambda q: (q.priority, q.subject, q.id))
    return questions


def serialize(questions: Optional[List[OpenQuestion]]) -> List[Dict[str, Any]]:
    return [q.as_dict() for q in (questions or [])]


def from_dicts(rows: Optional[List[Dict[str, Any]]]) -> List[OpenQuestion]:
    out: List[OpenQuestion] = []
    for r in rows or []:
        out.append(OpenQuestion(
            id=str(r.get("id", "")), priority=int(r.get("priority", 2) or 2),
            subject=str(r.get("subject", "")),
            question=str(r.get("question", "")), why=str(r.get("why", "")),
            suggested_tools=list(r.get("suggested_tools") or [])))
    return out


__all__ = ["OpenQuestion", "build_open_questions", "serialize", "from_dicts",
           "UNMAPPED_SHARE_HIGH", "ATTRIBUTION_PARTIAL", "MAX_AGREEMENT_LEADS"]
