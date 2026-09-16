"""Cross-check the ATPG tool's fault subclass against this tool's root cause.

Two independent classifications describe every coverage-loss fault:

* the **subclass** the ATPG tool wrote into the fault list (``AU.TC``,
  ``AU.PC``, ``UO.AAB`` ...), which is its own verdict on why the fault was
  not detected;
* the **root cause** this tool derived structurally from the netlist and the
  constraint file (``tied_or_constant_hardware``, ``scan_to_non_scan_boundary``
  ...).

Until now the two were never compared. Yet a fault the tool calls *tied*
(``AU.TC``) whose site this tool could not resolve to any constant, or an
*aborted* fault (``UO.AAB``) whose site this tool resolves to a hard tie, is
exactly the kind of lead a debug engineer wants first: one side is wrong, or
the design has a structure neither model captures.

Nothing here decides which side is right. It counts agreements, counts
disagreements, names the disagreeing pairs and leaves the judgement to the
reviewer -- the agent or the engineer -- with the evidence attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ..models import RootCause

#: The pair could not be judged because the site was never mapped.
NOT_MEASURED = "not_measured"
AGREE = "agree"
DISAGREE = "disagree"
#: The ATPG tool names a specific mechanism; this tool found nothing specific
#: (``other_structural_cause``). Not a contradiction -- a hole in this tool's
#: structural model, and the cheapest lead to check.
UNCONFIRMED = "unconfirmed"
#: Neither model says anything the other contradicts.
UNINFORMATIVE = "uninformative"

VERDICTS = (AGREE, DISAGREE, UNCONFIRMED, UNINFORMATIVE, NOT_MEASURED)

#: Verbatim samples kept per disagreeing pair.
SAMPLE_LIMIT = 5


@dataclass(frozen=True)
class Rule:
    """What this tool's root causes mean against one subclass family."""

    #: The subclass suffix after the family dot (``TC`` in ``AU.TC``), or the
    #: bare family when the tool wrote no subclass.
    suffix: str
    agree: FrozenSet[str]
    disagree: FrozenSet[str]
    #: Why a disagreement here is worth a look.
    lead: str


def _rc(*causes: RootCause) -> FrozenSet[str]:
    return frozenset(c.value for c in causes)


_CONSTRAINT = _rc(RootCause.CONSTRAINT_CONTROLLABILITY,
                  RootCause.CONSTRAINT_OBSERVABILITY)
_SCAN = _rc(RootCause.SCAN_TO_NON_SCAN, RootCause.NON_SCAN_PROPAGATION,
            RootCause.SCAN_NOT_CHAIN_CONNECTED)
_TIED = _rc(RootCause.TIED_CONSTANT)

#: One rule per subclass the tool is known to emit. A subclass absent here is
#: judged UNINFORMATIVE rather than guessed at.
RULES: Dict[str, Rule] = {
    "TC": Rule(
        "TC", agree=_TIED, disagree=_CONSTRAINT | _SCAN,
        lead=("The ATPG tool calls the site tied; this tool resolved no "
              "constant driver. Either a tie sits behind a black box or "
              "outside the parsed hierarchy, or the mapping landed on the "
              "wrong instance.")),
    "PC": Rule(
        "PC", agree=_CONSTRAINT | _rc(RootCause.CLOCK_RESET_TE_BLOCKING),
        disagree=_TIED | _SCAN,
        lead=("The ATPG tool attributes the loss to a pin constraint; this "
              "tool found no constraint on the cone. The constraint may live "
              "in a dofile that was not supplied, or inside an unevaluated "
              "conditional block.")),
    "SEQ": Rule(
        "SEQ", agree=_SCAN | _rc(RootCause.CLOCK_RESET_TE_BLOCKING),
        disagree=_TIED | _CONSTRAINT,
        lead=("The ATPG tool calls this sequential loss; this tool found a "
              "hard constant or a constraint instead. Check whether the "
              "non-scan element is being masked by something upstream.")),
    "AAB": Rule(
        "AAB", agree=frozenset(), disagree=_TIED | _CONSTRAINT,
        lead=("An aborted fault should be reachable in principle. A site "
              "this tool resolves to a tie or a constraint would normally "
              "be classified ATPG-untestable, not aborted -- the ATPG tool "
              "may be seeing a path this tool's structural model does not, "
              "or the mapping is wrong.")),
    "EAB": Rule(
        "EAB", agree=frozenset(), disagree=_TIED | _CONSTRAINT,
        lead=("Same as AAB: an aborted fault at a site this tool resolves to "
              "a hard blocker is a contradiction worth a look.")),
}


@dataclass
class PairCount:
    """One (subclass, root_cause) cell of the matrix."""

    subclass: str
    root_cause: str
    count: int = 0
    verdict: str = UNINFORMATIVE
    samples: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"subclass": self.subclass, "root_cause": self.root_cause,
                "count": self.count, "verdict": self.verdict,
                "samples": list(self.samples)}


@dataclass
class Agreement:
    """The full cross-check."""

    pairs: List[PairCount] = field(default_factory=list)
    totals: Dict[str, int] = field(default_factory=dict)
    #: Per subclass: how many of its measured faults agree.
    by_subclass: Dict[str, Dict[str, int]] = field(default_factory=dict)
    leads: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def disagreements(self) -> List[PairCount]:
        return sorted((p for p in self.pairs if p.verdict == DISAGREE),
                      key=lambda p: -p.count)

    @property
    def unconfirmed(self) -> List[PairCount]:
        return sorted((p for p in self.pairs if p.verdict == UNCONFIRMED),
                      key=lambda p: -p.count)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "totals": dict(self.totals),
            "by_subclass": {k: dict(v) for k, v in self.by_subclass.items()},
            "pairs": [p.as_dict() for p in self.pairs],
            "disagreements": [p.as_dict() for p in self.disagreements],
            "unconfirmed": [p.as_dict() for p in self.unconfirmed],
            "leads": [dict(x) for x in self.leads],
            "note": self.note,
            "verdict_meaning": {
                AGREE: "both classifications point at the same mechanism",
                DISAGREE: ("the two classifications contradict each other; "
                           "one is wrong or the structure is not modelled"),
                UNCONFIRMED: ("the ATPG tool names a mechanism this tool "
                              "could not find structurally; a hole in this "
                              "tool's model until checked"),
                UNINFORMATIVE: ("neither side says anything the other "
                                "contradicts"),
                NOT_MEASURED: ("the site never mapped onto the netlist, so "
                               "this tool has no root cause to compare"),
            },
        }


def _suffix(subclass: str) -> str:
    if "." in subclass:
        return subclass.split(".", 1)[1].upper()
    return subclass.upper()


def judge(subclass: str, root_cause: str, connectivity_known: bool) -> str:
    """Verdict for one fault's pair of classifications."""
    if not connectivity_known or root_cause == \
            RootCause.UNRESOLVED_CONNECTIVITY.value:
        return NOT_MEASURED
    rule = RULES.get(_suffix(subclass))
    if rule is None:
        return UNINFORMATIVE
    if root_cause in rule.agree:
        return AGREE
    if root_cause in rule.disagree:
        return DISAGREE
    if rule.agree and root_cause == RootCause.OTHER_STRUCTURAL.value:
        return UNCONFIRMED
    return UNINFORMATIVE


def cross_check(fault_results: Any) -> Agreement:
    """Build the subclass x root-cause matrix over *fault_results*."""
    cells: Dict[Tuple[str, str], PairCount] = {}
    totals = {v: 0 for v in VERDICTS}
    by_sub: Dict[str, Dict[str, int]] = {}

    for fr in (fault_results or []):
        fault = getattr(fr, "fault", None)
        if fault is None:
            continue
        subclass = str(getattr(fault, "dotted_class", None)
                       or getattr(getattr(fault, "fault_class", ""), "value",
                                  getattr(fault, "fault_class", "")) or "")
        root_cause = str(getattr(getattr(fr, "root_cause", ""), "value",
                                 getattr(fr, "root_cause", "")))
        known = bool(getattr(fr, "connectivity_known", True))
        verdict = judge(subclass, root_cause, known)

        cell = cells.get((subclass, root_cause))
        if cell is None:
            cell = PairCount(subclass, root_cause, verdict=verdict)
            cells[(subclass, root_cause)] = cell
        cell.count += 1
        if verdict in (DISAGREE, UNCONFIRMED) \
                and len(cell.samples) < SAMPLE_LIMIT:
            cell.samples.append(fault.fault_object)
        totals[verdict] += 1
        bucket = by_sub.setdefault(subclass, {v: 0 for v in VERDICTS})
        bucket[verdict] += 1

    pairs = sorted(cells.values(), key=lambda p: (-p.count, p.subclass,
                                                   p.root_cause))
    result = Agreement(pairs=pairs, totals=totals, by_subclass=by_sub)
    result.leads = _leads(result)
    result.note = _note(result)
    return result


def _leads(agreement: Agreement) -> List[Dict[str, Any]]:
    leads = []
    for pair in agreement.disagreements + agreement.unconfirmed:
        rule = RULES.get(_suffix(pair.subclass))
        if pair.verdict == DISAGREE:
            why = rule.lead if rule else ""
        else:
            why = (f"The ATPG tool classes these {pair.subclass}, naming a "
                   "specific mechanism; this tool's structural walk found "
                   "nothing specific at the site. Either the mechanism sits "
                   "outside the parsed hierarchy or behind a black box, or "
                   "this tool's model does not cover it. Check a sample.")
        leads.append({
            "subclass": pair.subclass,
            "root_cause": pair.root_cause,
            "verdict": pair.verdict,
            "count": pair.count,
            "samples": list(pair.samples),
            "why_it_matters": why,
            "suggested_tools": ["get_fault_detail", "why_blocked",
                                "scan_status", "list_constraints"],
        })
    return leads


def _note(agreement: Agreement) -> str:
    t = agreement.totals
    measured = t[AGREE] + t[DISAGREE] + t[UNCONFIRMED] + t[UNINFORMATIVE]
    if measured == 0:
        return ("No coverage-loss fault mapped onto the netlist, so the ATPG "
                "tool's subclasses could not be compared with a structural "
                "root cause.")
    parts = [
        f"{measured} mapped fault(s) compared: {t[AGREE]} agree, "
        f"{t[DISAGREE]} disagree, {t[UNCONFIRMED]} unconfirmed, "
        f"{t[UNINFORMATIVE]} uninformative; {t[NOT_MEASURED]} unmapped and "
        "not compared."
    ]
    if t[DISAGREE]:
        parts.append(
            f"{len(agreement.disagreements)} disagreeing pair(s) are listed "
            "as leads. A disagreement means one classification is wrong or "
            "the structure is not modelled -- it does not say which.")
    if t[UNCONFIRMED]:
        parts.append(
            f"{len(agreement.unconfirmed)} pair(s) are unconfirmed: the ATPG "
            "tool names a mechanism this tool could not find at the site.")
    if not t[DISAGREE] and not t[UNCONFIRMED]:
        parts.append("No pair contradicts the other; the structural picture "
                     "is consistent with the ATPG tool's own labels.")
    return " ".join(parts)


def from_dict(data: Optional[Dict[str, Any]]) -> Optional[Agreement]:
    """Rebuild an :class:`Agreement` from :meth:`Agreement.as_dict`."""
    if not data:
        return None
    pairs = [PairCount(p["subclass"], p["root_cause"],
                       int(p.get("count", 0)), p.get("verdict", UNINFORMATIVE),
                       list(p.get("samples") or []))
             for p in data.get("pairs", [])]
    return Agreement(pairs=pairs, totals=dict(data.get("totals") or {}),
                     by_subclass={k: dict(v) for k, v in
                                  (data.get("by_subclass") or {}).items()},
                     leads=[dict(x) for x in data.get("leads", [])],
                     note=str(data.get("note", "")))
