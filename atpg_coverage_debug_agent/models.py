"""Typed data models used across the ATPG coverage-debug agent.

All structures are plain :mod:`dataclasses` so they are trivial to serialise
into Markdown/CSV and easy to reason about in unit tests. They deliberately
preserve *raw* input strings alongside normalised forms so that no information
is silently lost during analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class FaultClass(str, Enum):
    """Tessent-style fault classification codes that we understand.

    Only a subset of the full Tessent fault dictionary is modelled here; the
    classes that matter for coverage-loss debugging are ``AU``, ``UO`` and
    ``UC``. Unknown codes are preserved verbatim via :class:`FaultRecord`.
    """

    DS = "DS"  # Detected by simulation
    DI = "DI"  # Detected by implication
    TI = "TI"  # Tied (constant) by hardware
    AU = "AU"  # ATPG untestable -> coverage loss
    UO = "UO"  # Unobserved -> coverage loss
    UC = "UC"  # Uncontrolled -> coverage loss
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_token(cls, token: str) -> "FaultClass":
        """Return the matching :class:`FaultClass` for *token* or ``UNKNOWN``."""
        token = (token or "").strip().upper()
        try:
            return cls(token)
        except ValueError:
            return cls.UNKNOWN


#: Fault classes that represent actual coverage loss we want to root-cause.
COVERAGE_LOSS_CLASSES = (FaultClass.AU, FaultClass.UO, FaultClass.UC)

#: Fault classes that count as detected coverage.
DETECTED_CLASSES = (FaultClass.DS, FaultClass.DI)


class MappingConfidence(str, Enum):
    """Confidence levels for fault-object -> netlist-object correlation."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


class RootCause(str, Enum):
    """Supported root-cause categories for coverage loss."""

    CONSTRAINT_CONTROLLABILITY = "constraint_induced_controllability_loss"
    CONSTRAINT_OBSERVABILITY = "constraint_induced_observability_loss"
    SCAN_TO_NON_SCAN = "scan_to_non_scan_boundary"
    NON_SCAN_PROPAGATION = "non_scan_blocks_propagation"
    TIED_OR_CONSTANT = "tied_or_constant_hardware"
    CLOCK_RESET_TE_BLOCKING = "clock_reset_or_test_enable_blocking"
    STRUCTURAL_MASKING = "structural_masking_or_reconvergence"
    UNRESOLVED_CONNECTIVITY = "unresolved_connectivity"
    OTHER_STRUCTURAL = "other_structural_cause"


# ---------------------------------------------------------------------------
# Netlist structural models
# ---------------------------------------------------------------------------
@dataclass
class Pin:
    """A pin on an instance (or a port on a module).

    Attributes:
        name: Logical pin name (e.g. ``A``, ``Y``, ``CK``).
        net: Name of the net connected to this pin, if any.
        direction: ``input``, ``output`` or ``unknown`` when not inferable.
    """

    name: str
    net: Optional[str] = None
    direction: str = "unknown"


@dataclass
class Instance:
    """A single instantiated cell within a module."""

    name: str
    cell_type: str
    module: str
    pins: List[Pin] = field(default_factory=list)
    #: Best-effort hierarchical path, populated during elaboration.
    hier_path: Optional[str] = None

    def pin_net(self, pin_name: str) -> Optional[str]:
        """Return the net attached to *pin_name* (case-insensitive)."""
        for pin in self.pins:
            if pin.name.lower() == pin_name.lower():
                return pin.net
        return None


@dataclass
class Net:
    """A net (wire) inside a module and its driver/load relationships."""

    name: str
    #: ``(instance_name, pin_name)`` tuples that drive this net.
    drivers: List[tuple] = field(default_factory=list)
    #: ``(instance_name, pin_name)`` tuples that load this net.
    loads: List[tuple] = field(default_factory=list)
    is_port: bool = False
    port_direction: Optional[str] = None


@dataclass
class Module:
    """A Verilog module definition with its instances and nets."""

    name: str
    ports: List[Pin] = field(default_factory=list)
    instances: Dict[str, Instance] = field(default_factory=dict)
    nets: Dict[str, Net] = field(default_factory=dict)

    def is_leaf(self) -> bool:
        """A module is a leaf if it instantiates no sub-modules we parsed."""
        return not self.instances


# ---------------------------------------------------------------------------
# Fault / constraint records
# ---------------------------------------------------------------------------
@dataclass
class FaultRecord:
    """A single line parsed from a Tessent fault list."""

    raw_text: str
    line_number: int
    fault_object: str
    normalized_object: str
    fault_class: FaultClass
    raw_class_token: str = ""
    fault_type: Optional[str] = None  # e.g. stuck-at value '0'/'1' when present

    @property
    def is_coverage_loss(self) -> bool:
        """True when this fault contributes to coverage loss."""
        return self.fault_class in COVERAGE_LOSS_CLASSES


@dataclass
class ConstraintRecord:
    """A structured representation of one constraint line."""

    raw_text: str
    line_number: int
    kind: str  # 'force', 'disable', 'block', 'constant', 'clock', 'reset', ...
    signal: Optional[str] = None
    normalized_signal: Optional[str] = None
    value: Optional[str] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Analysis results
# ---------------------------------------------------------------------------
@dataclass
class MappingResult:
    """Result of correlating a fault object to a netlist object."""

    fault_object: str
    normalized_object: str
    confidence: MappingConfidence
    instance_name: Optional[str] = None
    cell_type: Optional[str] = None
    matched_net: Optional[str] = None
    #: Alternative candidate matches we could not disambiguate.
    candidates: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)


@dataclass
class FaultAnalysisResult:
    """Full diagnosis for a single coverage-loss fault."""

    fault: FaultRecord
    mapping: MappingResult
    fan_in: List[str] = field(default_factory=list)
    fan_out: List[str] = field(default_factory=list)
    controllability_issue: bool = False
    observability_issue: bool = False
    constraint_related: bool = False
    scan_boundary_involved: bool = False
    root_cause: RootCause = RootCause.OTHER_STRUCTURAL
    #: Observed facts (things we measured directly from the inputs).
    observed_facts: List[str] = field(default_factory=list)
    #: Inferred conclusions (heuristic reasoning on top of observed facts).
    inferred_conclusions: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    recommended_step: str = ""

    @property
    def instance_name(self) -> Optional[str]:
        return self.mapping.instance_name

    @property
    def cell_type(self) -> Optional[str]:
        return self.mapping.cell_type


@dataclass
class PatternGroup:
    """A repeated-pattern grouping across many faults."""

    kind: str  # 'constraint', 'instance', 'module', 'boundary', 'root_cause'
    key: str
    count: int
    sample_faults: List[str] = field(default_factory=list)


@dataclass
class AnalysisSummary:
    """Executive summary produced by the summariser."""

    total_faults: int = 0
    class_counts: Dict[str, int] = field(default_factory=dict)
    subtype_counts: Dict[str, int] = field(default_factory=dict)
    coverage_loss_count: int = 0
    top_root_causes: List[tuple] = field(default_factory=list)
    top_instances: List[tuple] = field(default_factory=list)
    top_modules: List[tuple] = field(default_factory=list)
    top_constraints: List[tuple] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class AnalysisReport:
    """Top-level container bundling everything an analysis run produced."""

    summary: AnalysisSummary
    fault_results: List[FaultAnalysisResult] = field(default_factory=list)
    pattern_groups: List[PatternGroup] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skill_results: List[Any] = field(default_factory=list)
    # Source artefacts kept so the agentic AI layer can build a live skill
    # context and invoke skills as tools on demand. Optional; ``None`` when the
    # report was produced without retaining the parsed inputs.
    netlist: Any = None            # VerilogNetlist
    faults: Any = None             # List[FaultRecord]
    constraints: Any = None        # List[ConstraintRecord]
    #: Compact instance-name adjacency (set for reloaded reports whose live
    #: netlist object is gone, so path tracing still works).
    adjacency: Any = None
    #: Source metadata for the report cover header: design name and the
    #: netlist / faults / constraints file paths the analysis was run on.
    sources: Any = None
    #: Saved agent investigation: diagnosis text, chat transcript, and the
    #: tool-call / verification trace, so a reopened report is reproducible.
    investigation: Any = None
    #: Analyst edits applied to this report: excluded fault classes / ids and a
    #: note (set by the Edit Report action). ``None`` when unedited.
    edits: Any = None
