"""Static ATPG domain knowledge used by the offline analysis.

This package holds *knowledge*, not analysis: the Tessent fault-subclass
taxonomy (:mod:`.subclasses`) and the catalogue of candidate fixes
(:mod:`.fixes`). Nothing here reads files or inspects a design — the analysis
modules join this knowledge onto evidence derived from the netlist, fault list
and constraint file.
"""

from .fixes import FIX_CATALOG, FixAction, fixes_for_subclass
from .subclasses import SUBCLASS_CATALOG, SubclassInfo, describe_subclass

__all__ = [
    "FIX_CATALOG",
    "FixAction",
    "fixes_for_subclass",
    "SUBCLASS_CATALOG",
    "SubclassInfo",
    "describe_subclass",
]
