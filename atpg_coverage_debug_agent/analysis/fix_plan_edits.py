"""The agent's contribution to the fix plan, kept as an overlay.

The offline analysis ranks its fix proposals deterministically. After
reviewing them the agent may know something the catalogue does not -- that a
test data register is programmable from a specific dofile, that a what-if run
should precede an RTL change, or that a different action fits better. Until
now that knowledge lived in prose. This module lets the agent put it *into*
the plan through one tool, ``propose_fix``, with three actions:

``add``
    A new agent-authored proposal, appended after the offline entries.
``amend``
    A practical note attached to an existing offline entry. The offline
    rationale and commands stay verbatim; the note sits beside them.
``replace``
    The agent's proposal takes the offline entry's slot. The offline entry is
    **kept**, demoted to the end and marked superseded, so the plan the
    deterministic pass produced is always still readable.

The offline plan is never mutated. Edits are stored as records, persisted
with the investigation, and applied on every render by
:func:`apply_fix_plan_edits`. Every edit passes the same honesty checks as
the offline catalogue: no invented hierarchy path, no predicted coverage
gain, evidence required for anything that adds or replaces.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..knowledge.fixes import FixAction
from ..models import VerdictConfidence
from .recommend import Recommendation

logger = logging.getLogger(__name__)

#: File name inside the session directory (one JSON object per line).
FIX_EDITS_FILE = "fix_plan_edits.jsonl"

ACTIONS = ("add", "amend", "replace")
EFFORTS = ("low", "medium", "high")
RISKS = ("low", "medium", "high")
CONFIDENCES = tuple(c.value for c in VerdictConfidence)

#: Tag on every note and every agent-authored field, so a reader can never
#: mistake the agent's words for the catalogue's.
AGENT_TAG = "[agent]"


@dataclass
class FixPlanEdit:
    """One ``propose_fix`` call, exactly as the agent made it."""

    action: str
    subclass: str
    #: Offline rank this edit targets (``amend`` / ``replace``).
    target_rank: Optional[int] = None
    title: str = ""
    rationale: str = ""
    commands: List[str] = dc_field(default_factory=list)
    preconditions: List[str] = dc_field(default_factory=list)
    expected_effect: str = ""
    effort: str = "medium"
    risk: str = "low"
    note: str = ""
    reason: str = ""
    evidence: str = ""
    confidence: str = VerdictConfidence.MEDIUM.value
    recorded_at: str = dc_field(default_factory=lambda: time.strftime(
        "%Y-%m-%dT%H:%M:%S"))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action, "subclass": self.subclass,
            "target_rank": self.target_rank, "title": self.title,
            "rationale": self.rationale, "commands": list(self.commands),
            "preconditions": list(self.preconditions),
            "expected_effect": self.expected_effect, "effort": self.effort,
            "risk": self.risk, "note": self.note, "reason": self.reason,
            "evidence": self.evidence, "confidence": self.confidence,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FixPlanEdit":
        rank = d.get("target_rank")
        return cls(
            action=str(d.get("action", "add")),
            subclass=str(d.get("subclass", "")),
            target_rank=int(rank) if rank not in (None, "") else None,
            title=str(d.get("title", "") or ""),
            rationale=str(d.get("rationale", "") or ""),
            commands=[str(c) for c in (d.get("commands") or [])],
            preconditions=[str(p) for p in (d.get("preconditions") or [])],
            expected_effect=str(d.get("expected_effect", "") or ""),
            effort=str(d.get("effort", "medium") or "medium"),
            risk=str(d.get("risk", "low") or "low"),
            note=str(d.get("note", "") or ""),
            reason=str(d.get("reason", "") or ""),
            evidence=str(d.get("evidence", "") or ""),
            confidence=str(d.get("confidence", "medium") or "medium"),
            recorded_at=str(d.get("recorded_at", "") or ""),
        )


class FixEditsSink:
    """Collects edits in memory and, when given a path, appends each to a
    JSON-lines file so the GUI can read what the out-of-process server saw.

    Every follow-up turn starts a fresh server process, so the sink preloads
    what earlier turns recorded: the plan the agent is shown stays cumulative.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self.items: List[FixPlanEdit] = read_edits(path) if path else []

    def record(self, edit: FixPlanEdit) -> None:
        self.items.append(edit)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", mode=0o700,
                        exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(edit.as_dict(), default=str) + "\n")
        except OSError as exc:
            logger.warning("Fix-plan edit not persisted to %s: %s",
                           self.path, exc)

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [e.as_dict() for e in self.items]


def read_edits(path: Optional[str]) -> List[FixPlanEdit]:
    """Read every edit from a JSON-lines file; a missing file is empty."""
    if not path or not os.path.isfile(path):
        return []
    out: List[FixPlanEdit] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(FixPlanEdit.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError as exc:
        logger.warning("Could not read fix-plan edits from %s: %s", path, exc)
    return out


def edits_of(report: Any) -> List[FixPlanEdit]:
    """The edits recorded on *report* (in ``investigation``), typed."""
    investigation = getattr(report, "investigation", None) or {}
    if not isinstance(investigation, dict):
        return []
    rows = investigation.get("fix_plan_edits") or []
    out: List[FixPlanEdit] = []
    for row in rows:
        try:
            out.append(row if isinstance(row, FixPlanEdit)
                       else FixPlanEdit.from_dict(dict(row)))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Applying the overlay
# ---------------------------------------------------------------------------
def _split_commands(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.rstrip() for line in value.splitlines() if line.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _agent_action(edit: FixPlanEdit, index: int) -> FixAction:
    return FixAction(
        fix_id=f"agent_{index}",
        title=f"{AGENT_TAG} {edit.title}".strip(),
        applies_to=[edit.subclass],
        rationale=edit.rationale,
        preconditions=list(edit.preconditions),
        commands=list(edit.commands),
        expected_effect=edit.expected_effect,
        effort=edit.effort,
        risk=edit.risk,
        # Never estimated: the same rule the offline catalogue lives by.
        requires_measurement=True,
        feasibility_rank=2,
        caveat="Proposed by the AI agent during its review; verify before "
               "acting. The offline plan is unchanged.",
    )


def _confidence(value: str) -> VerdictConfidence:
    try:
        return VerdictConfidence(value)
    except ValueError:
        return VerdictConfidence.MEDIUM


def _agent_recommendation(edit: FixPlanEdit, index: int,
                          like: Optional[Recommendation]) -> Recommendation:
    return Recommendation(
        rank=0,
        subclass_id=edit.subclass,
        fault_count=like.fault_count if like else 0,
        pct=like.pct if like else 0.0,
        fix=_agent_action(edit, index),
        confidence=_confidence(edit.confidence),
        evidence=[f"{AGENT_TAG} {edit.evidence}"] if edit.evidence else [],
        caveats=["Agent proposal: the benefit must be measured by a re-run; "
                 "no coverage gain is predicted."],
        actionable=like.actionable if like else "partial",
        hotspot=like.hotspot if like else "",
        origin="agent",
        edit_reason=edit.reason,
    )


def apply_fix_plan_edits(base: Sequence[Recommendation],
                         edits: Iterable[FixPlanEdit]) -> List[Recommendation]:
    """Return the plan a reader should see: *base* with *edits* overlaid.

    *base* is not modified. Amendments attach notes; additions append;
    replacements take the slot and demote the original to the tail, marked
    superseded. Ranks are renumbered on the result so the list reads 1..N.
    """
    plan: List[Recommendation] = [
        replace(rec, evidence=list(rec.evidence), caveats=list(rec.caveats),
                agent_notes=list(rec.agent_notes))
        for rec in base]
    by_rank = {rec.rank: rec for rec in plan}
    demoted: List[Recommendation] = []
    additions: List[Recommendation] = []
    original_rank: Dict[int, int] = {id(rec): rec.rank for rec in plan}

    for index, edit in enumerate(edits, start=1):
        target = by_rank.get(edit.target_rank) if edit.target_rank else None
        if edit.action == "amend":
            if target is None:
                continue
            target.agent_notes.append(f"{AGENT_TAG} {edit.note}".strip())
        elif edit.action == "add":
            like = next((r for r in plan
                         if r.subclass_id.upper() == edit.subclass.upper()),
                        None)
            additions.append(_agent_recommendation(edit, index, like))
        elif edit.action == "replace":
            if target is None or target in demoted:
                continue
            new = _agent_recommendation(edit, index, target)
            pos = plan.index(target)
            plan[pos] = new
            demoted.append(target)
            new.supersedes = original_rank[id(target)]
            # Marker resolved after renumbering below.
            target.superseded_by = -1
            original_rank[id(new)] = new.supersedes
            by_rank[edit.target_rank] = new
        else:
            continue

    result = plan + additions + demoted
    for rank, rec in enumerate(result, start=1):
        rec.rank = rank
    # Point each demoted entry at the agent proposal that took its slot.
    for rec in demoted:
        winner = next((r for r in result if r.origin == "agent"
                       and r.supersedes == original_rank[id(rec)]), None)
        rec.superseded_by = winner.rank if winner else None
        if winner is not None and winner.edit_reason:
            rec.agent_notes.append(
                f"{AGENT_TAG} superseded by proposal #{winner.rank}: "
                f"{winner.edit_reason}")
    return result


def effective_plan(report: Any) -> List[Recommendation]:
    """The offline plan on *report* with the agent's edits applied."""
    base = list(getattr(report, "recommendations", None) or [])
    edits = edits_of(report)
    if not edits:
        return base
    return apply_fix_plan_edits(base, edits)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------
def _known_subclasses(triage: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Upper-cased id -> id as written, from the triage payload."""
    known: Dict[str, str] = {}
    stats = (triage or {}).get("statistics") or {}
    for row in stats.get("subclasses") or []:
        if isinstance(row, dict) and row.get("subclass"):
            known[str(row["subclass"]).upper()] = str(row["subclass"])
    for sel in (triage or {}).get("selected") or []:
        if isinstance(sel, dict) and sel.get("subclass"):
            known[str(sel["subclass"]).upper()] = str(sel["subclass"])
    for rec in (triage or {}).get("recommendations") or []:
        if isinstance(rec, dict) and rec.get("subclass"):
            known[str(rec["subclass"]).upper()] = str(rec["subclass"])
    return known


def _guardrail_issues(texts: Iterable[str], fault_results: Any,
                      constraints: Any) -> List[str]:
    from .guardrails import PathRegistry, check_text, issues_as_warnings
    registry = PathRegistry.from_parts(fault_results=fault_results or (),
                                       constraints=constraints or ())
    issues = []
    for text in texts:
        if text:
            issues.extend(check_text(text, registry, "propose_fix"))
    return issues_as_warnings(issues)


def propose_fix(sink: Optional[FixEditsSink], args: Dict[str, Any],
                triage: Optional[Dict[str, Any]] = None,
                fault_results: Any = None,
                constraints: Any = None) -> Dict[str, Any]:
    """Validate *args* and record a :class:`FixPlanEdit` into *sink*.

    Refuses anything the offline catalogue would refuse of itself: a
    hierarchy path that is not in the inputs, an elided path, a predicted
    coverage gain. Returns the plan the reader will now see so the agent can
    check its own edit landed where it meant.
    """
    action = str(args.get("action", "") or "").strip().lower()
    if action not in ACTIONS:
        return {"error": f"action must be one of {', '.join(ACTIONS)}; got "
                         f"{action!r}."}
    if not triage:
        return {"error": "No fix plan in this session; run an analysis first."}

    known = _known_subclasses(triage)
    offline = [r for r in (triage.get("recommendations") or [])
               if isinstance(r, dict)]
    by_rank = {int(r.get("rank", 0)): r for r in offline}

    target_rank: Optional[int] = None
    subclass = str(args.get("subclass", "") or "").strip()
    if action in ("amend", "replace"):
        raw = args.get("target_rank", args.get("rank"))
        try:
            target_rank = int(raw)
        except (TypeError, ValueError):
            return {"error": f"{action} needs target_rank: the rank of the "
                             "offline fix-plan entry it applies to.",
                    "available_ranks": sorted(by_rank)}
        if target_rank not in by_rank:
            return {"error": f"No offline fix-plan entry has rank "
                             f"{target_rank}.",
                    "available_ranks": sorted(by_rank)}
        subclass = str(by_rank[target_rank].get("subclass", subclass))
    if not subclass:
        return {"error": "subclass is required (the category the fix is for)."}
    if known and subclass.upper() not in known:
        return {"error": f"subclass {subclass!r} is not a category in this "
                         "session.", "available_subclasses": sorted(known.values())}
    subclass = known.get(subclass.upper(), subclass)

    title = str(args.get("title", "") or "").strip()
    rationale = str(args.get("rationale", "") or "").strip()
    note = str(args.get("note", "") or "").strip()
    reason = str(args.get("reason", "") or "").strip()
    evidence = str(args.get("evidence", "") or "").strip()
    expected = str(args.get("expected_effect", "") or "").strip()
    effort = str(args.get("effort", "medium") or "medium").lower()
    risk = str(args.get("risk", "low") or "low").lower()
    confidence = str(args.get("confidence", "medium") or "medium").lower()
    commands = _split_commands(args.get("commands"))
    preconditions = _split_commands(args.get("preconditions"))

    if action == "amend":
        if not note:
            return {"error": "amend needs note: the practical note to attach."}
    else:
        missing = [k for k, v in (("title", title), ("rationale", rationale),
                                  ("evidence", evidence)) if not v]
        if action == "replace" and not reason:
            missing.append("reason")
        if missing:
            return {"error": f"{action} needs {', '.join(missing)}."}
    if effort not in EFFORTS or risk not in RISKS:
        return {"error": f"effort must be one of {', '.join(EFFORTS)} and "
                         f"risk one of {', '.join(RISKS)}."}
    if confidence not in CONFIDENCES:
        return {"error": f"confidence must be one of "
                         f"{', '.join(CONFIDENCES)}."}

    problems = _guardrail_issues(
        [title, rationale, note, reason, expected, evidence] + commands
        + preconditions, fault_results, constraints)
    if problems:
        return {"error": "The proposal fails the same honesty checks the "
                         "offline plan is held to. Fix these and retry.",
                "issues": problems}

    edit = FixPlanEdit(action=action, subclass=subclass,
                       target_rank=target_rank, title=title,
                       rationale=rationale, commands=commands,
                       preconditions=preconditions, expected_effect=expected,
                       effort=effort, risk=risk, note=note, reason=reason,
                       evidence=evidence, confidence=confidence)
    if sink is None:
        return {"error": "No fix-plan sink in this session; the proposal was "
                         "not recorded."}
    sink.record(edit)

    # Show the agent the plan as the reader will now see it.
    base = _recommendations_from_payload(offline)
    plan = apply_fix_plan_edits(base, sink.items)
    return {
        "recorded": True,
        "edit": edit.as_dict(),
        "edits_so_far": len(sink.items),
        "plan": [{"rank": r.rank, "subclass": r.subclass_id,
                  "title": r.title, "origin": r.origin,
                  "superseded_by": r.superseded_by,
                  "agent_notes": list(r.agent_notes)} for r in plan],
        "note": ("The offline entry is never overwritten: an amendment sits "
                 "beside it, a replacement demotes it (still visible) and "
                 "an addition is appended. No coverage gain is predicted."),
    }


def _recommendations_from_payload(rows: List[Dict[str, Any]]
                                  ) -> List[Recommendation]:
    """Rebuild lightweight Recommendations from the serialised triage payload
    (the out-of-process server has no live objects)."""
    out: List[Recommendation] = []
    for row in rows:
        fix = FixAction(
            fix_id=str(row.get("fix_id", "")),
            title=str(row.get("title", "")),
            applies_to=[str(row.get("subclass", ""))],
            rationale=str(row.get("rationale", "")),
            preconditions=[str(p) for p in (row.get("preconditions") or [])],
            commands=[str(c) for c in (row.get("commands") or [])],
            expected_effect=str(row.get("expected_effect", "") or ""),
            effort=str(row.get("effort", "medium") or "medium"),
            risk=str(row.get("risk", "low") or "low"),
            requires_measurement=bool(row.get("requires_measurement", True)),
        )
        out.append(Recommendation(
            rank=int(row.get("rank", 0) or 0),
            subclass_id=str(row.get("subclass", "")),
            fault_count=int(row.get("fault_count", 0) or 0),
            pct=float(row.get("pct", 0.0) or 0.0),
            fix=fix,
            confidence=_confidence(str(row.get("confidence", "medium"))),
            evidence=[str(e) for e in (row.get("evidence") or [])],
            caveats=[str(c) for c in (row.get("caveats") or [])],
            actionable=str(row.get("actionable", "partial") or "partial"),
            hotspot=str(row.get("hotspot", "") or ""),
            origin=str(row.get("origin", "offline") or "offline"),
        ))
    return out
