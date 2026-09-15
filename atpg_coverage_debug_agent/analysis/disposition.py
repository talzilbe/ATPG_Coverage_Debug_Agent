"""Detect whether a fault list is a pre- or post-disposition snapshot.

A Tessent flow commonly runs a fault-disposition step after the last ATPG
phase. Its generic shape is::

    do_fault_disposition <fault_model>
    write_faults <file> ... -change_class <WAIVER_SUBCLASS>
    set_relevant_coverage -exclude <WAIVER_SUBCLASS>
    read_faults <waiver file> -delete

which produces the tool's two-column report: a *total* column over the full
population and a *total relevant* column with the waived subclass excluded.

The step does not merely move the totals -- it **rewrites the AU subclass
distribution**. Category ranking and the fix plan are built from those subclass
counts, so analysing a per-phase snapshot can put the wrong category at the top
of the plan. Detection counts are typically unchanged; it is the classification
of the *undetected* population that moves.

Nothing here parses a log or assumes a filename. The verdict comes from the
fault list's own contents -- whether the waiver subclass is present -- and
file names are used only to *rank* candidates and to explain a verdict. Every
pattern is configurable in :mod:`..config.analysis_config`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config.analysis_config import AnalysisConfig, LOSS_ROLES, resolve

logger = logging.getLogger(__name__)

#: The analysed list already carries the waiver subclass.
STATE_POST = "post_disposition"
#: The analysed list predates the disposition step.
STATE_PRE = "pre_disposition"
#: Not enough evidence to say. Reported as-is; never guessed either way.
STATE_UNDETERMINED = "undetermined"

#: Human-readable labels, so every surface words the state identically.
STATE_LABELS = {
    STATE_POST: "post-disposition",
    STATE_PRE: "pre-disposition",
    STATE_UNDETERMINED: "undetermined",
}

#: Appended whenever a pre-disposition snapshot drives the triage.
PRE_DISPOSITION_WARNING = (
    "This fault list predates the run's fault-disposition step. The "
    "disposition rewrites the ATPG-untestable subclass distribution, so the "
    "category ranking and the fix plan below may not reflect the final design "
    "state. Re-run against the post-disposition fault list before acting on "
    "the ranking."
)

#: Appended when no relevant population could be formed.
NO_WAIVER_NOTE = (
    "No disposition waiver subclass was found in this fault list, so there is "
    "one population and the total column is the only column."
)


@dataclass
class FaultListCandidate:
    """One file in the fault-list directory that could be the population."""

    path: str
    phase: Optional[int] = None
    named_post_disposition: bool = False
    partial: bool = False
    mtime: float = 0.0
    size: int = 0

    @property
    def name(self) -> str:
        """Base name, which is all the ranking ever looks at."""
        return os.path.basename(self.path)

    @property
    def rank(self) -> Tuple[int, int, float, int]:
        """Sort key, best last.

        A name that spells out the final/disposition list wins; otherwise the
        latest phase wins; ties break on modification time and then size. Size
        is last so that two copies of the same list pick deterministically.
        """
        return (1 if self.named_post_disposition else 0,
                self.phase if self.phase is not None else -1,
                self.mtime, self.size)

    def as_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "name": self.name, "phase": self.phase,
                "named_post_disposition": self.named_post_disposition,
                "partial": self.partial, "size": self.size}


@dataclass
class DispositionState:
    """What snapshot of the fault population was analysed, and how we know."""

    state: str = STATE_UNDETERMINED
    waiver_subclass: Optional[str] = None
    waiver_count: int = 0
    resolved_path: Optional[str] = None
    phase: Optional[int] = None
    #: Tagged reasons, in the order they were established.
    evidence: List[str] = field(default_factory=list)
    #: Other fault lists sitting in the same directory.
    candidates: List[FaultListCandidate] = field(default_factory=list)
    #: A candidate that looks closer to the final state than what was read.
    better_candidate: Optional[str] = None
    #: Waiver subclasses that matched nothing but could not be ruled out.
    waiver_candidates: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: Per-subclass count change against another snapshot, when one was read.
    subclass_delta: Dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """The state in words, for a report header."""
        return STATE_LABELS.get(self.state, self.state)

    @property
    def is_pre(self) -> bool:
        return self.state == STATE_PRE

    @property
    def has_relevant_population(self) -> bool:
        """True when a waiver subclass was found and can form a second column."""
        return bool(self.waiver_subclass) and self.waiver_count > 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "label": self.label,
            "waiver_subclass": self.waiver_subclass,
            "waiver_count": self.waiver_count,
            "resolved_path": self.resolved_path,
            "phase": self.phase,
            "evidence": list(self.evidence),
            "candidates": [c.as_dict() for c in self.candidates],
            "better_candidate": self.better_candidate,
            "waiver_candidates": list(self.waiver_candidates),
            "warnings": list(self.warnings),
            "subclass_delta": dict(self.subclass_delta),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]
                  ) -> Optional["DispositionState"]:
        """Rebuild from :meth:`as_dict`; ``None`` passes through."""
        if not data:
            return None
        state = cls(
            state=str(data.get("state") or STATE_UNDETERMINED),
            waiver_subclass=data.get("waiver_subclass") or None,
            waiver_count=int(data.get("waiver_count") or 0),
            resolved_path=data.get("resolved_path") or None,
            phase=data.get("phase"),
            evidence=list(data.get("evidence") or []),
            better_candidate=data.get("better_candidate") or None,
            waiver_candidates=list(data.get("waiver_candidates") or []),
            warnings=list(data.get("warnings") or []),
            subclass_delta=dict(data.get("subclass_delta") or {}),
        )
        state.candidates = [
            FaultListCandidate(path=str(row.get("path") or ""),
                               phase=row.get("phase"),
                               named_post_disposition=bool(
                                   row.get("named_post_disposition")),
                               partial=bool(row.get("partial")),
                               size=int(row.get("size") or 0))
            for row in (data.get("candidates") or [])
        ]
        return state


# ---------------------------------------------------------------------------
# Waiver-subclass discovery
# ---------------------------------------------------------------------------
def discover_waiver_subclass(statistics: Any,
                             config: Optional[AnalysisConfig] = None
                             ) -> Tuple[Optional[str], List[str], List[str]]:
    """Find the disposition waiver subclass in an observed population.

    The waiver subclass is *discovered*, never assumed: the run chooses it, and
    a different flow spells it differently. Resolution order:

    1. A subclass present in the fault list that matches a configured waiver
       glob. This is the only case that yields an answer.
    2. Otherwise nothing is adopted, and any subclass the subclass catalogue
       does not describe is returned as a *candidate* for a human to confirm.

    Returns:
        ``(waiver_subclass, evidence, candidates)``. ``waiver_subclass`` is
        ``None`` when nothing matched.
    """
    config = resolve(config)
    evidence: List[str] = []
    candidates: List[str] = []

    observed = [s for s in getattr(statistics, "subclass_stats", []) or []
                if getattr(s, "count", 0) > 0]
    if not observed:
        return None, ["[fault_list] the population is empty"], []

    matched = [s for s in observed if config.is_waiver_subclass(s.subclass_id)]
    if matched:
        best = max(matched, key=lambda s: s.count)
        evidence.append(
            f"[fault_list] subclass {best.subclass_id} ({best.count} fault(s)) "
            f"matches a configured waiver pattern "
            f"({', '.join(config.waiver_subclass_patterns)})")
        if len(matched) > 1:
            others = ", ".join(f"{s.subclass_id}={s.count}"
                               for s in matched if s is not best)
            evidence.append(
                f"[fault_list] other subclasses also matched ({others}); the "
                f"largest was taken and the rest left in the population")
        return best.subclass_id, evidence, [s.subclass_id for s in matched]

    # Nothing matched. Offer the undescribed subclasses rather than inventing
    # a waiver: adopting the wrong one would silently delete real faults.
    # Only a coverage-loss role can be a waiver bucket -- the step exists to
    # take faults OUT of the coverage denominator, so a detected-by-implication
    # subtype such as DI.<something> is never a candidate however new it looks.
    # The lookup is EXACT: describe_subclass() falls back to the family, which
    # would describe every unseen subclass and leave no candidates at all.
    from ..knowledge.subclasses import SUBCLASS_CATALOG
    for stat in observed:
        if "." not in stat.subclass_id:
            continue
        if config.role_of(stat.subclass_id) not in LOSS_ROLES:
            continue
        if stat.subclass_id.strip().upper() not in SUBCLASS_CATALOG:
            candidates.append(stat.subclass_id)
    if candidates:
        evidence.append(
            "[fault_list] no subclass matched a waiver pattern; undescribed "
            f"coverage-loss subclass(es) present: {', '.join(candidates)}")
    else:
        evidence.append(
            "[fault_list] no subclass matched a waiver pattern and every "
            "coverage-loss subclass present is a documented one")
    return None, evidence, candidates


# ---------------------------------------------------------------------------
# Fault-list file discovery
# ---------------------------------------------------------------------------
def find_fault_list_candidates(directory: str,
                               config: Optional[AnalysisConfig] = None
                               ) -> List[FaultListCandidate]:
    """Every file under *directory* that looks like a complete fault list.

    Discovery is by pattern relative to the directory, never by literal name,
    so a different design, phase count or naming scheme needs no code change.
    Partial files (a waiver block, a detected-only slice) are excluded: one of
    those standing in for the population would silently change every metric.
    """
    config = resolve(config)
    found: List[FaultListCandidate] = []
    if not directory or not os.path.isdir(directory):
        return found

    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        logger.warning("Cannot list fault-list directory %s (%s)",
                       directory, exc)
        return found

    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        if not config.looks_like_fault_list(name):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        found.append(FaultListCandidate(
            path=path,
            phase=config.phase_of(name),
            named_post_disposition=config.looks_post_disposition(name),
            partial=config.looks_partial(name),
            mtime=stat.st_mtime,
            size=stat.st_size,
        ))
    found.sort(key=lambda c: c.rank, reverse=True)
    return found


def select_fault_list(path: str, config: Optional[AnalysisConfig] = None
                      ) -> Tuple[str, List[FaultListCandidate], List[str]]:
    """Resolve *path* to the fault list to analyse.

    A **directory** is resolved to its best candidate. An explicit **file** is
    always honoured -- silently analysing something the caller did not name is
    worse than a wrong default -- but a better-looking sibling is reported so
    the choice is visible.

    Returns:
        ``(resolved_path, candidates, warnings)``.
    """
    config = resolve(config)
    warnings: List[str] = []

    if path and os.path.isdir(path):
        candidates = find_fault_list_candidates(path, config)
        if not candidates:
            warnings.append(
                f"No file under {path} matched a fault-list pattern "
                f"({', '.join(config.fault_list_file_patterns)}).")
            return path, candidates, warnings
        chosen = candidates[0]
        warnings.append(
            f"Selected {chosen.name} from {len(candidates)} fault-list "
            f"candidate(s) in {path}.")
        return chosen.path, candidates, warnings

    directory = os.path.dirname(os.path.abspath(path)) if path else ""
    candidates = find_fault_list_candidates(directory, config)
    return path, candidates, warnings


def _better_candidate(resolved: str,
                      candidates: List[FaultListCandidate],
                      config: AnalysisConfig) -> Optional[FaultListCandidate]:
    """A sibling that looks closer to the final state than *resolved*."""
    current = os.path.basename(resolved or "")
    if not current:
        return None
    if config.looks_post_disposition(current):
        return None
    for candidate in candidates:
        if candidate.name == current or candidate.partial:
            continue
        if candidate.named_post_disposition:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def classify_snapshot(path: str, statistics: Any,
                      candidates: Optional[List[FaultListCandidate]] = None,
                      config: Optional[AnalysisConfig] = None
                      ) -> DispositionState:
    """Decide whether the analysed fault list is pre- or post-disposition.

    Contents decide. The waiver subclass being *present* is proof the
    disposition already ran. Its absence is only suggestive, so the filename
    shape breaks the tie: a per-phase tag means a mid-flow snapshot, anything
    else leaves the verdict ``undetermined`` rather than guessed.
    """
    config = resolve(config)
    candidates = list(candidates or [])
    name = os.path.basename(path or "")

    waiver, evidence, waiver_candidates = discover_waiver_subclass(
        statistics, config)
    phase = config.phase_of(name)

    state = DispositionState(
        waiver_subclass=waiver,
        resolved_path=path or None,
        phase=phase,
        evidence=evidence,
        candidates=candidates,
        waiver_candidates=waiver_candidates,
    )

    if waiver:
        stat = statistics.get(waiver) if hasattr(statistics, "get") else None
        state.waiver_count = int(getattr(stat, "count", 0) or 0)
        state.state = STATE_POST
        state.evidence.append(
            f"[fault_list] the waiver subclass is present, so the "
            f"disposition step already ran on this list")
    elif phase is not None:
        state.state = STATE_PRE
        state.evidence.append(
            f"[filename] {name} carries phase tag {phase} and holds no waiver "
            f"subclass, so it is a per-phase snapshot taken before the "
            f"disposition step")
    else:
        state.state = STATE_UNDETERMINED
        state.evidence.append(
            f"[filename] {name or 'the fault list'} carries no phase tag and "
            f"the population holds no waiver subclass; the snapshot cannot be "
            f"placed relative to the disposition step from the data available")

    better = _better_candidate(path, candidates, config)
    if better is not None and state.state != STATE_POST:
        state.better_candidate = better.path
        state.warnings.append(
            f"A fault list that looks post-disposition sits beside the one "
            f"analysed: {better.name}. The analysed file "
            f"({name or path}) was kept because it was named explicitly.")

    if state.is_pre:
        state.warnings.append(PRE_DISPOSITION_WARNING)
    elif state.state == STATE_UNDETERMINED:
        state.warnings.append(
            "Whether this fault list is pre- or post-disposition could not be "
            "determined. Treat the category ranking as provisional.")
    if not waiver and waiver_candidates:
        state.warnings.append(
            f"No configured waiver pattern matched. Subclass(es) "
            f"{', '.join(waiver_candidates)} are present but undocumented; if "
            f"one of them is this run's disposition bucket, add it to "
            f"waiver_subclass_patterns so the relevant column can be built.")
    return state


def subclass_delta(before: Any, after: Any) -> Dict[str, int]:
    """Per-subclass count change from *before* to *after*.

    Shows what the disposition step actually did to the classification of the
    undetected population -- which is the part that moves a fix plan.
    """
    def _counts(stats: Any) -> Dict[str, int]:
        return {s.subclass_id: s.count
                for s in getattr(stats, "subclass_stats", []) or []}

    old, new = _counts(before), _counts(after)
    delta = {}
    for key in sorted(set(old) | set(new)):
        change = new.get(key, 0) - old.get(key, 0)
        if change:
            delta[key] = change
    return delta
