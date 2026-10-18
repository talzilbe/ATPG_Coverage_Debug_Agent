"""Find the fault list and constraints that belong with a netlist.

An ATPG run keeps its inputs in one tree (for example a SAGE ``WORK`` area
with ``<recipe>/netlist``, ``<recipe>/faultlist`` and
``proj_common/atpg_files``), but the netlist is not always at the same depth.
The search therefore starts in the netlist's folder and climbs, trying the
configured relative patterns at each level; the nearest level that matches
wins. Which fault list is chosen reuses the disposition ranking, so the GUI and
the analysis agree on what "the final list" means.

Nothing here reads file contents: the GUI's own input check verifies whatever
is filled in.
"""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from ..config.analysis_config import AnalysisConfig, resolve
from .disposition import FaultListCandidate, find_fault_list_candidates

logger = logging.getLogger(__name__)

_COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz", ".z")
_NETLIST_SUFFIXES = (".v", ".vg", ".sv", ".verilog", ".vnet", ".vlg")
_DOFILE_SUFFIXES = (".do", ".dofile", ".tcl")


@dataclass
class InputChoice:
    """One input picked for the user, with why, and what else was there."""

    path: str
    reason: str
    directory: str
    #: Other usable files in :attr:`directory`, best first.
    alternatives: List[str] = field(default_factory=list)
    #: Other folders that also held candidates.
    other_dirs: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class InputDiscovery:
    netlist: str
    stem: str
    faults: Optional[InputChoice] = None
    constraints: Optional[InputChoice] = None


def design_stem(netlist_path: str) -> str:
    """The design name a netlist file is named after: ``top.v.gz`` -> ``top``."""
    name = os.path.basename((netlist_path or "").strip())
    lower = name.lower()
    for suffix in _COMPRESSION_SUFFIXES:
        if lower.endswith(suffix):
            name, lower = name[:-len(suffix)], lower[:-len(suffix)]
            break
    for suffix in _NETLIST_SUFFIXES:
        if lower.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name


def _ancestors(netlist_path: str, levels: int) -> List[str]:
    current = os.path.dirname(os.path.abspath(netlist_path))
    found = [current]
    for _ in range(max(0, levels)):
        parent = os.path.dirname(current)
        if parent == current:
            break
        found.append(parent)
        current = parent
    return found


def _glob(root: str, pattern: str) -> List[str]:
    return sorted(glob.glob(os.path.join(glob.escape(root), pattern)))


def _named_after(name: str, stem: str) -> bool:
    if not stem:
        return False
    name, stem = name.lower(), stem.lower()
    return name == stem or name.startswith(stem + ".") or name.startswith(stem + "_")


def _prefer_stem(candidates: Sequence[FaultListCandidate],
                 stem: str) -> List[FaultListCandidate]:
    # Stable: the disposition rank still orders files within each group.
    return sorted(candidates, key=lambda c: not _named_after(c.name, stem))


def _fault_reason(chosen: FaultListCandidate, ordered: Sequence[FaultListCandidate],
                  stem: str) -> str:
    if chosen.named_post_disposition:
        tag = " ('.fd' in its name)" if ".fd" in chosen.name.lower() else ""
        reason = f"{chosen.name} is the final fault list{tag}"
    elif chosen.phase is not None:
        reason = (f"{chosen.name} is from the latest ATPG phase (phase "
                  f"{chosen.phase}); no final ('.fd') fault list was found")
    elif len(ordered) > 1:
        reason = f"{chosen.name} is the most recently written fault list"
    else:
        reason = f"{chosen.name} is the only fault list there"
    if stem and not _named_after(chosen.name, stem):
        reason += f"; none there is named after the design '{stem}'"
    return reason


def find_fault_list(netlist_path: str, config: Optional[AnalysisConfig] = None
                    ) -> Optional[InputChoice]:
    """The fault list that most likely belongs with *netlist_path*, or None."""
    config = resolve(config)
    stem = design_stem(netlist_path)
    for level, root in enumerate(_ancestors(netlist_path, config.discovery_max_levels)):
        scored: List[Tuple[str, List[FaultListCandidate]]] = []
        seen = set()
        for pattern in config.fault_list_dir_globs:
            for directory in _glob(root, pattern):
                if directory in seen or not os.path.isdir(directory):
                    continue
                seen.add(directory)
                candidates = find_fault_list_candidates(directory, config)
                if level > 0:
                    # Above the netlist's own folder, unrelated runs are in reach.
                    candidates = [c for c in candidates if _named_after(c.name, stem)]
                if candidates:
                    scored.append((directory, _prefer_stem(candidates, stem)))
        if not scored:
            continue
        directory, ordered = max(
            scored, key=lambda s: (_named_after(s[1][0].name, stem), s[1][0].rank))
        chosen = ordered[0]
        others = [d for d, _ in scored if d != directory]
        alternatives = [c.path for c in ordered[1:]]
        reason = _fault_reason(chosen, ordered, stem)
        note = f"Auto-filled from the netlist's run folder: {reason}. "
        if alternatives:
            note += (f"{len(alternatives)} other fault list(s) are in {directory} "
                     "\u2014 use 'Pick another' or edit the path.")
        else:
            note += f"It is the only fault list in {directory}."
        if others:
            note += (f" {len(others)} other fault-list folder(s) were found next "
                     f"to it, e.g. {others[0]}.")
        return InputChoice(chosen.path, reason, directory, alternatives, others, note)
    return None


def find_constraints(netlist_path: str, config: Optional[AnalysisConfig] = None
                     ) -> Optional[InputChoice]:
    """The constraints dofile named after the design, or None."""
    config = resolve(config)
    stem = design_stem(netlist_path)
    if not stem:
        return None
    for root in _ancestors(netlist_path, config.discovery_max_levels):
        for pattern in config.constraint_file_globs:
            matches = [m for m in _glob(root, pattern.replace("{stem}", glob.escape(stem)))
                       if os.path.isfile(m)]
            if not matches:
                continue
            matches.sort(key=_mtime, reverse=True)
            chosen = matches[0]
            directory = os.path.dirname(chosen)
            siblings = [os.path.join(directory, n) for n in _listdir(directory)
                        if n.lower().endswith(_DOFILE_SUFFIXES)]
            alternatives = matches[1:] + [p for p in siblings
                                          if p != chosen and p not in matches]
            name = os.path.basename(chosen)
            reason = f"{name} is named after the design '{stem}'"
            note = f"Auto-filled from the netlist's run folder: {reason}."
            if alternatives:
                note += (f" {len(alternatives)} other dofile(s) are in {directory}; "
                         "edit the path to use another one.")
            return InputChoice(chosen, reason, directory, alternatives, [], note)
    return None


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _listdir(directory: str) -> List[str]:
    try:
        return sorted(n for n in os.listdir(directory)
                      if os.path.isfile(os.path.join(directory, n)))
    except OSError:
        return []


def discover_inputs(netlist_path: str, config: Optional[AnalysisConfig] = None
                    ) -> InputDiscovery:
    """Both inputs for *netlist_path*; a failed search leaves that input None."""
    result = InputDiscovery(netlist=netlist_path, stem=design_stem(netlist_path))
    if not (netlist_path or "").strip():
        return result
    for attr, finder in (("faults", find_fault_list),
                         ("constraints", find_constraints)):
        try:
            setattr(result, attr, finder(netlist_path, config))
        except OSError as exc:
            logger.warning("Input search for %s failed: %s", attr, exc)
    return result
