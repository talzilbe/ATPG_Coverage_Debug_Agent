"""The complete fault census, and the self-check that proves it is complete.

Every other module that prints fault-class counts renders *this* object. That
is the whole point: the defect this module exists to prevent was not a wrong
number, it was **two different views of the same population, one of them a
silent subset**.

What happened
-------------
A digest listed six fault classes and, three lines above, a grand total. The
six classes were the coverage-loss-relevant ones; five legitimate Tessent
classes were filtered out and never mentioned. Nothing said the list was a
subset, so the reader closed the arithmetic, found a residual, and invented a
fault category to hold it. The residual was never missing data — it was in
memory the whole time.

The rules this module enforces
------------------------------
1. **A census is complete or it is labelled.** :meth:`FaultCensus.subset_note`
   produces the marker any partial listing must carry: how many classes of how
   many, how many faults of how many, and where the complete form lives.
   Never emit a subset and a grand total in the same block without
   reconciling them.
2. **Grouping is derived, never hardcoded.** Roles come from the configurable
   class-role map; families come from the class token itself. A class token
   the configuration has never heard of appears under the explicit
   :data:`UNCLASSIFIED` role with its verbatim name. It is never dropped and
   never folded into a neighbour.
3. **The sum is checked on every run, not only in tests.**
   :func:`census_warnings` re-adds the whole census and reports the delta and
   the tokens involved if it does not reconcile. A residual is a defect to
   report, never something to absorb.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config.analysis_config import AnalysisConfig, CoverageRole, resolve

logger = logging.getLogger(__name__)

#: Role label used for any class token the configuration does not map. Kept
#: distinct from ``CoverageRole.UNKNOWN`` in the rendered output so a reader
#: sees a bucket that demands attention rather than a shrug.
UNCLASSIFIED = "UNCLASSIFIED"

#: Role render order: detected, possibly detected, undetectable, ATPG
#: untestable, not detected, then anything unrecognised.
ROLE_ORDER: Sequence[str] = (
    CoverageRole.DT.value,
    CoverageRole.PD.value,
    CoverageRole.UD.value,
    CoverageRole.AU.value,
    CoverageRole.ND.value,
    UNCLASSIFIED,
)

#: Human label per role.
ROLE_LABELS: Dict[str, str] = {
    CoverageRole.DT.value: "Detected",
    CoverageRole.PD.value: "Possibly detected",
    CoverageRole.UD.value: "Undetectable",
    CoverageRole.AU.value: "ATPG untestable",
    CoverageRole.ND.value: "Not detected",
    UNCLASSIFIED: "Unclassified",
}

#: Why a role is or is not a debug target. Stated for *every* role, because
#: "these classes are out of scope" is only honest when the reason is printed
#: next to the count. ``BL`` and ``RE`` faults are real and were previously
#: invisible; they are in scope of the census even though they are not in
#: scope of the triage.
ROLE_SCOPE: Dict[str, str] = {
    CoverageRole.DT.value: (
        "Detected by the pattern set. Not a debug target; counted here so the "
        "census reconciles and the coverage metrics can be re-derived."),
    CoverageRole.PD.value: (
        "Possibly detected. Credited at the configured posdet factor in the "
        "coverage metrics, never treated as full detection."),
    CoverageRole.UD.value: (
        "Proven undetectable by the tool (unused, tied, blocked or "
        "redundant). Removed from the test-coverage denominator and NOT a "
        "debug target: no pattern can detect them. They are listed because "
        "an unexpectedly large undetectable population is itself a finding, "
        "and because omitting them is what makes a test-coverage figure "
        "wrong."),
    CoverageRole.AU.value: (
        "ATPG untestable: the tool could not prove a test exists under the "
        "current constraints and design state. A primary debug target."),
    CoverageRole.ND.value: (
        "Not detected -- aborted or left unprocessed rather than proven "
        "untestable. A primary debug target."),
    UNCLASSIFIED: (
        "The class token is not in the configured class-role map, so these "
        "faults contribute to NO coverage metric. Add the token to "
        "'class_roles' in the analysis configuration; until then any metric "
        "derived from this run understates its own denominator."),
}


@dataclass
class CensusEntry:
    """One dotted fault class and its counts.

    Attributes:
        subclass: Dotted class id verbatim from the fault list, e.g. ``DI.CLK``.
        family: The token before the first ``.``, e.g. ``DI``.
        role: Coverage role, or :data:`UNCLASSIFIED`.
        count: Faults in this class.
        pct: Share of the whole fault population, in percent.
        sa0 / sa1: Stuck-at split, where the fault list recorded one.
    """

    subclass: str
    family: str
    role: str
    count: int
    pct: float = 0.0
    sa0: int = 0
    sa1: int = 0

    @property
    def recognised(self) -> bool:
        return self.role != UNCLASSIFIED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subclass": self.subclass,
            "family": self.family,
            "role": self.role,
            "count": self.count,
            "pct": round(self.pct, 4),
            "sa0": self.sa0,
            "sa1": self.sa1,
        }


@dataclass
class CensusFamily:
    """A coarse class (``DI``) and the dotted subclasses beneath it."""

    family: str
    role: str
    entries: List[CensusEntry] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(e.count for e in self.entries)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "role": self.role,
            "count": self.count,
            "subclasses": [e.as_dict() for e in self.entries],
        }


@dataclass
class CensusRole:
    """One coverage role with its families, in report order."""

    role: str
    label: str
    scope: str
    families: List[CensusFamily] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(f.count for f in self.families)

    @property
    def entries(self) -> List[CensusEntry]:
        return [e for f in self.families for e in f.entries]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "label": self.label,
            "scope": self.scope,
            "count": self.count,
            "families": [f.as_dict() for f in self.families],
        }


@dataclass
class FaultCensus:
    """Every fault class in the run, grouped role -> family -> subclass.

    Attributes:
        total_faults: The population the census must account for.
        roles: Roles in :data:`ROLE_ORDER`, empty ones omitted.
        entries: Flat list of every class, largest first.
        basis: Where the counts came from, for the audit trail.
        posdet_credit: The credit factor the metrics applied to ``PD``.
    """

    total_faults: int = 0
    roles: List[CensusRole] = field(default_factory=list)
    entries: List[CensusEntry] = field(default_factory=list)
    basis: str = "fault_list"
    posdet_credit: float = 0.5

    # -- reconciliation ---------------------------------------------------
    @property
    def counted(self) -> int:
        """Sum of every class count in the census."""
        return sum(e.count for e in self.entries)

    @property
    def delta(self) -> int:
        """Faults the census fails to account for. Zero when it reconciles."""
        return self.total_faults - self.counted

    @property
    def reconciles(self) -> bool:
        """True when the census sums exactly to :attr:`total_faults`."""
        return self.delta == 0

    @property
    def class_count(self) -> int:
        """How many distinct classes the census holds."""
        return len(self.entries)

    @property
    def unclassified(self) -> List[CensusEntry]:
        """Classes the configuration could not place in a coverage role."""
        return [e for e in self.entries if not e.recognised]

    def role_count(self, role: str) -> int:
        """Faults carrying *role*."""
        return sum(e.count for e in self.entries if e.role == role)

    def get(self, subclass: str) -> Optional[CensusEntry]:
        """Return the entry for *subclass*, case-insensitively."""
        key = (subclass or "").strip().upper()
        for entry in self.entries:
            if entry.subclass.upper() == key:
                return entry
        return None

    # -- subset labelling -------------------------------------------------
    def subset_note(self, shown: Iterable[str], where: str) -> str:
        """Return the marker a partial listing of this census must carry.

        Args:
            shown: The class tokens the caller actually printed.
            where: Where the complete census can be read instead.

        Returns:
            An empty string when *shown* covers the whole census, otherwise a
            marker naming how many classes and how many faults are missing
            and where to find them. Callers must print whatever comes back.
        """
        keys = {str(s).strip().upper() for s in shown}
        listed = [e for e in self.entries if e.subclass.upper() in keys]
        if len(listed) >= self.class_count:
            return ""
        faults = sum(e.count for e in listed)
        return (
            f"PARTIAL LIST -- {len(listed)} of {self.class_count} classes, "
            f"{faults} of {self.total_faults} faults. This is a subset, not "
            f"the census; the remaining {self.total_faults - faults} fault(s) "
            f"are not missing. Complete census: {where}."
        )

    def reconciliation(self) -> Dict[str, Any]:
        """The self-check result, for machines."""
        return {
            "total_faults": self.total_faults,
            "sum_of_class_counts": self.counted,
            "delta": self.delta,
            "reconciles": self.reconciles,
            "classes": self.class_count,
            "unclassified_tokens": [e.subclass for e in self.unclassified],
        }

    def as_dict(self) -> Dict[str, Any]:
        """Counts-only serialisation. Deliberately carries no samples.

        Small enough to travel inline in every tool response, which is the
        property that makes an unexplained residual impossible.
        """
        return {
            "complete": True,
            "basis": self.basis,
            "posdet_credit": self.posdet_credit,
            "reconciliation": self.reconciliation(),
            "roles": [r.as_dict() for r in self.roles],
            "classes": [e.as_dict() for e in self.entries],
            "note": (
                "Every fault class parsed from the fault list, grouped by "
                "coverage role. sum(classes[].count) == "
                "reconciliation.total_faults. If it does not, call "
                "report_handoff_gap -- do not name the residual yourself."),
        }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def _role_of(subclass: str, declared: str,
             config: AnalysisConfig) -> str:
    """Resolve the render role for a class, preferring what was recorded.

    *declared* is the role the statistics pass already stored. It is trusted
    when it names a real role so a reloaded session renders exactly as it did
    when it was produced; otherwise the class is looked up in the live
    configuration, and anything still unplaced becomes :data:`UNCLASSIFIED`.
    """
    value = (declared or "").strip().upper()
    if value and value not in (CoverageRole.UNKNOWN.value, UNCLASSIFIED):
        return value
    resolved = config.role_of(subclass)
    if resolved is CoverageRole.UNKNOWN:
        return UNCLASSIFIED
    return resolved.value


def _family_of(subclass: str, declared: str, config: AnalysisConfig) -> str:
    family = (declared or "").strip()
    if family:
        return family
    return config.family_of(subclass) or subclass


def _assemble(entries: List[CensusEntry], total: int, basis: str,
              posdet_credit: float) -> FaultCensus:
    """Group *entries* into roles and families, in report order."""
    for entry in entries:
        entry.pct = (100.0 * entry.count / total) if total else 0.0

    roles: List[CensusRole] = []
    # Any role present but not named in ROLE_ORDER still gets rendered, after
    # the known ones. A configuration may legitimately introduce a role name
    # this module was not written against.
    order = list(ROLE_ORDER) + sorted(
        {e.role for e in entries} - set(ROLE_ORDER))
    for role in order:
        members = [e for e in entries if e.role == role]
        if not members:
            continue
        families: Dict[str, CensusFamily] = {}
        for entry in members:
            fam = families.get(entry.family)
            if fam is None:
                fam = CensusFamily(family=entry.family, role=role)
                families[entry.family] = fam
            fam.entries.append(entry)
        for fam in families.values():
            fam.entries.sort(key=lambda e: (-e.count, e.subclass))
        roles.append(CensusRole(
            role=role,
            label=ROLE_LABELS.get(role, role),
            scope=ROLE_SCOPE.get(role, ""),
            families=sorted(families.values(),
                            key=lambda f: (-f.count, f.family)),
        ))

    entries.sort(key=lambda e: (-e.count, e.subclass))
    return FaultCensus(total_faults=total, roles=roles, entries=entries,
                       basis=basis, posdet_credit=posdet_credit)


def build_census(report: Any,
                 config: Optional[AnalysisConfig] = None) -> FaultCensus:
    """Build the complete census for *report*.

    Reads the derived statistics when the report has them, which carry the
    stuck-at split and the role each class was actually counted under. Falls
    back to the summary's sub-class counts, then to its coarse class counts,
    so a report produced before the statistics pass existed still renders a
    complete census rather than nothing at all.

    Args:
        report: An ``AnalysisReport``, a ``DerivedStatistics``, or ``None``.
        config: Analysis configuration; the active one is used when omitted.

    Returns:
        A :class:`FaultCensus`. An empty or missing report yields an empty
        census whose :attr:`FaultCensus.reconciles` is ``True``.
    """
    config = resolve(config)
    if report is None:
        return FaultCensus(posdet_credit=config.posdet_credit)

    stats = getattr(report, "statistics", report)
    if stats is not None and getattr(stats, "subclass_stats", None) is not None:
        entries = [
            CensusEntry(
                subclass=s.subclass_id,
                family=_family_of(s.subclass_id, s.family, config),
                role=_role_of(s.subclass_id, s.role, config),
                count=s.count,
                sa0=s.sa0,
                sa1=s.sa1,
            )
            for s in stats.subclass_stats if s.count
        ]
        total = int(getattr(stats, "total_faults", 0) or 0)
        # The summary is the population of record: it is what the report's
        # headline "total faults analysed" comes from. Reconciling against it
        # rather than against the statistics' own total is what catches a
        # census that is internally consistent but describes a different
        # population than the one on the cover.
        summary = getattr(report, "summary", None)
        if summary is not None:
            total = int(getattr(summary, "total_faults", total) or total)
        return _assemble(entries, total, "derived_statistics",
                         float(getattr(stats, "posdet_credit",
                                       config.posdet_credit)))

    summary = getattr(report, "summary", None)
    if summary is None:
        return FaultCensus(posdet_credit=config.posdet_credit)

    counts: Dict[str, int] = dict(getattr(summary, "subtype_counts", None) or {})
    basis = "summary.subtype_counts"
    if not counts:
        counts = dict(getattr(summary, "class_counts", None) or {})
        basis = "summary.class_counts"
    entries = [
        CensusEntry(
            subclass=token,
            family=_family_of(token, "", config),
            role=_role_of(token, "", config),
            count=int(count),
        )
        for token, count in counts.items() if count
    ]
    return _assemble(entries, int(getattr(summary, "total_faults", 0) or 0),
                     basis, config.posdet_credit)


# ---------------------------------------------------------------------------
# The self-check
# ---------------------------------------------------------------------------
def census_warnings(census: FaultCensus) -> List[str]:
    """Assert the census reconciles, and describe the failure when it does not.

    Runs on every analysis, not only under test. Two distinct problems are
    reported separately because they have different fixes:

    * a **residual** -- the class counts do not sum to the population, which
      is a defect in this tool and must never be absorbed into a bucket;
    * **unclassified classes** -- the fault list carries a class the
      configuration does not map, which is a configuration gap and silently
      removes those faults from every coverage metric.

    Returns:
        Warning strings, empty when the census is complete and fully mapped.
    """
    issues: List[str] = []
    if not census.reconciles:
        tokens = ", ".join(f"{e.subclass}={e.count}"
                           for e in census.entries[:10]) or "none"
        issues.append(
            f"CENSUS MISMATCH: the {census.class_count} fault class(es) sum "
            f"to {census.counted} but {census.total_faults} fault(s) were "
            f"analysed, leaving a delta of {census.delta}. No number derived "
            f"from this census is trustworthy until the delta is attributed. "
            f"Classes counted (largest first): {tokens}."
        )
    unclassified = census.unclassified
    if unclassified:
        listed = ", ".join(f"{e.subclass}={e.count}" for e in unclassified)
        total = sum(e.count for e in unclassified)
        issues.append(
            f"UNCLASSIFIED FAULT CLASS(ES): {listed} ({total} fault(s), "
            f"{100.0 * total / census.total_faults:.2f}% of the list) are not "
            f"in the configured class-role map, so they contribute to no "
            f"coverage metric and the test-coverage denominator is "
            f"understated. Map them via 'class_roles' in the analysis "
            f"configuration."
            if census.total_faults else
            f"UNCLASSIFIED FAULT CLASS(ES): {listed}."
        )
    for issue in issues:
        logger.warning(issue)
    return issues


def counts_by_role(census: FaultCensus) -> "Counter[str]":
    """Faults per role, for callers that want the totals without the tree."""
    counter: Counter = Counter()
    for entry in census.entries:
        counter[entry.role] += entry.count
    return counter
