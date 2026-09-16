"""Structured findings the agent records while reviewing the offline analysis.

The agent's answer used to be free text, so a correction it made ("this site
is tied, not a scan boundary") lived only in a transcript. Nothing in the
report, the table or the next run learned from it. This module gives the
agent one tool -- ``record_finding`` -- that writes a typed record, and gives
the GUI and the reports one place to read those records back.

A finding never overwrites an offline value. It sits beside it, labelled with
who said it and on what evidence, so a reader always sees both and the
offline analysis stays reproducible.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: File name inside the session directory (one JSON object per line).
FINDINGS_FILE = "findings.jsonl"

#: What kind of statement a finding makes.
KINDS = ("correction", "confirmation", "new_lead", "gap")
KIND_MEANING = {
    "correction": "the offline value is wrong; agent_value is the corrected one",
    "confirmation": "the offline value was checked with tools and holds",
    "new_lead": "something the offline analysis did not surface at all",
    "gap": "the evidence available does not settle this (say what would)",
}

#: Which offline field the finding is about. ``other`` is allowed but a
#: named field lets the GUI place the note next to the value it corrects.
FIELDS = ("root_cause", "scan_status", "tie_driver", "blocking_source",
          "category_ranking", "fix_plan", "mapping", "other")

CONFIDENCES = ("high", "medium", "low", "insufficient")


@dataclass
class Finding:
    """One typed statement from the agent about the offline analysis."""

    kind: str
    subject: str
    field: str = "other"
    offline_value: str = ""
    agent_value: str = ""
    evidence: str = ""
    confidence: str = "medium"
    subject_verified: bool = False
    recorded_at: str = dc_field(default_factory=lambda: time.strftime(
        "%Y-%m-%dT%H:%M:%S"))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "subject": self.subject, "field": self.field,
            "offline_value": self.offline_value,
            "agent_value": self.agent_value, "evidence": self.evidence,
            "confidence": self.confidence,
            "subject_verified": self.subject_verified,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Finding":
        return cls(
            kind=str(d.get("kind", "new_lead")),
            subject=str(d.get("subject", "")),
            field=str(d.get("field", "other") or "other"),
            offline_value=str(d.get("offline_value", "") or ""),
            agent_value=str(d.get("agent_value", "") or ""),
            evidence=str(d.get("evidence", "") or ""),
            confidence=str(d.get("confidence", "medium") or "medium"),
            subject_verified=bool(d.get("subject_verified", False)),
            recorded_at=str(d.get("recorded_at", "") or ""),
        )


class FindingsSink:
    """Collects findings in memory and, when given a path, appends each one
    to a JSON-lines file so another process (the GUI) can read them."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        # A follow-up turn runs in a fresh server process; start from what
        # earlier turns already recorded so counts stay cumulative.
        self.items: List[Finding] = read_findings(path) if path else []

    def record(self, finding: Finding) -> None:
        self.items.append(finding)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", mode=0o700,
                        exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(finding.as_dict(), default=str) + "\n")
        except OSError as exc:
            logger.warning("Finding not persisted to %s: %s", self.path, exc)

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [f.as_dict() for f in self.items]


def read_findings(path: Optional[str]) -> List[Finding]:
    """Read every finding from a JSON-lines file; a missing file is empty."""
    if not path or not os.path.isfile(path):
        return []
    out: List[Finding] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Finding.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
    except OSError as exc:
        logger.warning("Could not read findings from %s: %s", path, exc)
    return out


def _known_subjects(fault_results: Any, triage: Optional[Dict[str, Any]]
                    ) -> tuple:
    faults = set()
    for fr in (fault_results or []):
        fault = getattr(fr, "fault", None)
        if fault is not None:
            faults.add(str(getattr(fault, "fault_object", "")))
    subclasses = set()
    stats = (triage or {}).get("statistics") or {}
    for key in ("subclasses", "classes"):
        rows = stats.get(key)
        if isinstance(rows, dict):
            subclasses.update(k.upper() for k in rows)
        elif isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("subclass"):
                    subclasses.add(str(row["subclass"]).upper())
    for sel in (triage or {}).get("selected") or []:
        if isinstance(sel, dict) and sel.get("subclass"):
            subclasses.add(str(sel["subclass"]).upper())
    return faults, subclasses


def record_finding(sink: Optional[FindingsSink], args: Dict[str, Any],
                   fault_results: Any = None,
                   triage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate *args* and record a :class:`Finding` into *sink*.

    The subject is checked against the faults and categories in this session
    so a finding about an object that does not exist is flagged at the point
    it is made rather than discovered in the answer.
    """
    kind = str(args.get("kind", "") or "").strip().lower()
    subject = str(args.get("subject", "") or "").strip()
    if kind not in KINDS:
        return {"error": f"kind must be one of {', '.join(KINDS)}; got "
                         f"{kind!r}.", "kinds": dict(KIND_MEANING)}
    if not subject:
        return {"error": "subject is required: the fault path, category id "
                         "or report section the finding is about."}
    fld = str(args.get("field", "other") or "other").strip().lower()
    if fld not in FIELDS:
        return {"error": f"field must be one of {', '.join(FIELDS)}; got "
                         f"{fld!r}."}
    confidence = str(args.get("confidence", "medium") or "medium").lower()
    if confidence not in CONFIDENCES:
        return {"error": f"confidence must be one of "
                         f"{', '.join(CONFIDENCES)}; got {confidence!r}."}
    if kind == "correction" and not str(args.get("agent_value", "")).strip():
        return {"error": "a correction must state agent_value: what the "
                         "value should be."}
    evidence = str(args.get("evidence", "") or "").strip()
    if kind in ("correction", "new_lead") and not evidence:
        return {"error": f"a {kind} must cite evidence: which tool result "
                         "or report section supports it."}

    faults, subclasses = _known_subjects(fault_results, triage)
    verified = subject in faults or subject.upper() in subclasses
    finding = Finding(kind=kind, subject=subject, field=fld,
                      offline_value=str(args.get("offline_value", "") or ""),
                      agent_value=str(args.get("agent_value", "") or ""),
                      evidence=evidence, confidence=confidence,
                      subject_verified=verified)
    if sink is None:
        return {"error": "No findings sink in this session; the finding was "
                         "not recorded."}
    sink.record(finding)
    out = {"recorded": True, "finding": finding.as_dict(),
           "count": len(sink.items)}
    if not verified:
        out["warning"] = (
            "subject matches no fault path or category id in this session. "
            "It was recorded, but quote it only if it is a report section or "
            "a design object you verified with verify_paths.")
    return out


def summarize(findings: Iterable[Finding]) -> Dict[str, Any]:
    """Counts by kind and by field, for the reports."""
    by_kind: Dict[str, int] = {}
    by_field: Dict[str, int] = {}
    total = 0
    for f in findings:
        total += 1
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        by_field[f.field] = by_field.get(f.field, 0) + 1
    return {"total": total, "by_kind": by_kind, "by_field": by_field}
