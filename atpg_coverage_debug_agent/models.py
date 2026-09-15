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
    """Tessent fault classification codes the analyzer models explicitly.

    The full standard stuck-at set is present, not just the coverage-loss
    subset. An incomplete enum here is what previously let five legitimate
    classes (``UU``, ``BL``, ``RE``, ``PT``, ``PU``) fall into ``UNKNOWN``
    without a warning and corrupt every derived metric.

    A class that this enum does not name is *not* silently reclassified: the
    verbatim token is kept in :attr:`FaultRecord.raw_class_token`, the record
    is reported by :mod:`..diagnostics`, and the coverage role still comes
    from :mod:`..config.analysis_config`, which is overridable per partition.
    """

    DS = "DS"  # Detected by simulation
    DI = "DI"  # Detected by implication
    PT = "PT"  # Possibly detected, testable
    PU = "PU"  # Possibly detected, untestable
    UU = "UU"  # Undetectable, unused
    TI = "TI"  # Undetectable, tied (constant) by hardware
    BL = "BL"  # Undetectable, blocked
    RE = "RE"  # Undetectable, redundant
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
#:
#: Kept as a constant for readability only. The authoritative decision is
#: :meth:`..config.analysis_config.AnalysisConfig.is_loss_class`, which is
#: driven by the configurable role map so a partition can onboard a class this
#: tuple has never heard of.
COVERAGE_LOSS_CLASSES = (FaultClass.AU, FaultClass.UO, FaultClass.UC)

#: Fault classes that count as detected coverage.
DETECTED_CLASSES = (FaultClass.DS, FaultClass.DI)


class MappingConfidence(str, Enum):
    """Confidence levels for fault-object -> netlist-object correlation."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


class EvidenceSource(str, Enum):
    """Where a piece of evidence came from, strongest first.

    Recording the source keeps a fact stated by the ATPG tool distinct from
    one this analyzer inferred structurally. Only the first three are direct
    readings of an input file; the last two are this tool's own reasoning and
    must never be presented with the authority of a tool report.
    """

    FAULT_LIST = "fault_list"
    CONSTRAINT_FILE = "constraint_file"
    NETLIST = "netlist"
    STRUCTURAL_INFERENCE = "structural_inference"
    CLUSTERING_HINT = "clustering_hint"


class VerdictConfidence(str, Enum):
    """How much weight a diagnosis or recommendation should carry.

    ``INSUFFICIENT`` is a legitimate outcome: reporting that the available
    evidence does not support a conclusion is preferable to guessing.
    """

    HIGH = "high"
    MEDIUM = "medium"
    REDUCED = "reduced"
    INSUFFICIENT = "insufficient"


class RootCause(str, Enum):
    """Supported root-cause categories for coverage loss."""

    CONSTRAINT_CONTROLLABILITY = "constraint_induced_controllability_loss"
    CONSTRAINT_OBSERVABILITY = "constraint_induced_observability_loss"
    SCAN_TO_NON_SCAN = "scan_to_non_scan_boundary"
    NON_SCAN_PROPAGATION = "non_scan_blocks_propagation"
    #: The cell has a scan-data input and a shift-enable, so it is scannable,
    #: but its scan-in and/or scan-out binds to a dangling net: it was never
    #: stitched into a chain. Structurally distinct from a scan/non-scan
    #: boundary and fixed differently (re-stitch, not add a wrapper), so it
    #: must not be folded into the boundary categories.
    SCAN_NOT_CHAIN_CONNECTED = "scan_capable_but_not_chain_connected"
    TIED_OR_CONSTANT = "tied_or_constant_hardware"
    #: Alias: the ``tied_constant`` bucket. A fault site whose resolved driver
    #: is a tie cell belongs here and NOT in ``other_structural_cause`` -- it
    #: is expected and non-actionable, and mixing the two hides real targets.
    TIED_CONSTANT = "tied_or_constant_hardware"
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
    #: Verbatim instantiation text as it appears in the netlist, including
    #: every continuation line. Any claim about this cell's pins must be
    #: quotable from here rather than paraphrased.
    source_text: str = ""
    #: 1-based line number of the instantiation in the netlist file.
    line_number: Optional[int] = None

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
    #: 1-based line number where the module body starts.
    line_number: Optional[int] = None

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
        """True when this fault contributes to debuggable coverage loss.

        Decided from the configurable coverage-role map rather than a
        hard-coded class tuple, so a partition that introduces a new class
        only needs a configuration entry. Roles ``AU`` and ``ND`` are loss;
        ``DT``, ``PD`` and ``UD`` are not, and an unrecognised class is never
        counted as loss because that would be an inference, not a reading.
        """
        from .config.analysis_config import get_config
        return get_config().is_loss_class(self.dotted_class)

    @property
    def coverage_role(self) -> str:
        """Coverage role of this fault (``DT``/``PD``/``UD``/``AU``/``ND``)."""
        from .config.analysis_config import get_config
        return get_config().role_of(self.dotted_class).value

    @property
    def subclass(self) -> Optional[str]:
        """Dotted subtype from the class token, or ``None`` when absent.

        ``AU.TC`` -> ``TC``, ``UO.AAB`` -> ``AAB``, plain ``AU`` -> ``None``.
        Tessent's subclass *is* its own root-cause label, so this is the single
        highest-value signal available from the fault list alone.
        """
        token = (self.raw_class_token or "").strip()
        if "." not in token:
            return None
        sub = token.split(".", 1)[1].strip().upper()
        return sub or None

    @property
    def dotted_class(self) -> str:
        """Canonical ``CLASS`` or ``CLASS.SUB`` identifier for this fault.

        When the class family is one this build does not model, the verbatim
        token from the fault list is used rather than the string ``UNKNOWN``,
        so an unrecognised class stays visible and inspectable instead of
        merging with every other unrecognised class.
        """
        base = self.fault_class.value
        if self.fault_class is FaultClass.UNKNOWN and self.raw_class_token:
            return self.raw_class_token.strip().upper()
        sub = self.subclass
        return f"{base}.{sub}" if sub else base

    @property
    def sa_key(self) -> Optional[str]:
        """``'sa0'`` / ``'sa1'`` derived from the stuck value, else ``None``."""
        if self.fault_type in ("0", "1"):
            return f"sa{self.fault_type}"
        return None


@dataclass
class ConstraintRecord:
    """A structured representation of one constraint directive.

    The first six fields are the historic shape and are what fault matching
    uses. The rest record what the dofile grammar understood, so a reader can
    tell "no constraint affects this fault" apart from "the constraint file
    could not be parsed" — presenting the second as the first turns a parser
    limitation into a claim about the design.
    """

    raw_text: str
    line_number: int
    kind: str  # 'force', 'disable', 'block', 'constant', 'clock', 'reset', ...
    signal: Optional[str] = None
    normalized_signal: Optional[str] = None
    value: Optional[str] = None
    notes: str = ""
    #: Verbatim command name as written, e.g. ``add_cell_constraints``.
    directive: str = ""
    #: Option flags and their values, e.g. ``{"-hier": "", "-pulse_always": ""}``.
    options: Dict[str, str] = field(default_factory=dict)
    #: Every object this directive applies to, after collection expansion.
    targets: List[str] = field(default_factory=list)
    #: False when the directive was recognised but could not be evaluated
    #: (an unexpanded variable, an unresolvable collection, a condition).
    resolved: bool = True
    #: File the directive came from, which differs from the top-level
    #: constraint file once ``dofile`` includes are followed.
    source_file: str = ""
    #: True when the directive sits inside an ``if``/``else`` branch that was
    #: not evaluated, so it may or may not apply to a given run.
    conditional: bool = False


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
    #: Module the matched instance was defined in. Leaf instance names repeat
    #: across replicated modules, so the module is what makes the match
    #: unambiguous.
    module_name: Optional[str] = None
    #: Pin segment of the fault object, when one was identified.
    pin_name: Optional[str] = None
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
    #: Terminal driver of the fault site, resolved across hierarchy feedthrough
    #: ports (``analysis.connectivity.DriverResolution``). ``None`` means the
    #: driver was not resolved -- never that there is no driver.
    driver_resolution: Any = None
    #: Scan status of the mapped cell taken from its pin list:
    #: ``scan`` / ``non_scan`` / ``unknown``. ``unknown`` is the value whenever
    #: no instantiation was read, and it must never be rendered as ``non_scan``.
    scan_cell_state: str = "unknown"
    #: Verbatim instantiation the scan verdict was read from, when there is one.
    scan_evidence: str = ""
    #: Tie driver found for this site, as a serialisable dict, else ``None``.
    tie_driver: Optional[Dict[str, Any]] = None
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

    @property
    def connectivity_known(self) -> bool:
        """True only when this fault object mapped onto a netlist instance.

        When the mapping is ``UNRESOLVED`` the analyzer never located the
        object, so it holds **no** connectivity information at all. The
        fan-in/fan-out lists are empty for that reason and not because the
        node is unconnected — the two situations must never be reported with
        the same value.
        """
        return self.mapping.confidence is not MappingConfidence.UNRESOLVED

    @property
    def fan_in_count(self) -> Optional[int]:
        """Immediate fan-in size, or ``None`` when connectivity is unknown.

        ``None`` (rendered as NULL/unknown, never ``0``) is mandatory here: a
        reader who sees ``0`` will conclude the node has no drivers, which is
        a claim the analyzer cannot make about an unmapped object.
        """
        return len(self.fan_in) if self.connectivity_known else None

    @property
    def fan_out_count(self) -> Optional[int]:
        """Immediate fan-out size, or ``None`` when connectivity is unknown."""
        return len(self.fan_out) if self.connectivity_known else None

    @property
    def scan_boundary_state(self) -> str:
        """``'yes'`` / ``'no'`` / ``'unknown'`` for the scan-boundary column.

        ``'no'`` means "searched the neighbourhood and found no boundary";
        ``'unknown'`` means "never located the object, so nothing was
        searched". Collapsing the second into the first is what lets a reader
        mistake a mapping failure for proof of non-scan logic.
        """
        if not self.connectivity_known:
            return "unknown"
        return "yes" if self.scan_boundary_involved else "no"


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
    # -- evidence quality -------------------------------------------------
    # How much of the coverage loss rests on evidence that was actually read,
    # as opposed to rows the analyzer never managed to map. Reported openly so
    # a conclusion drawn from a contaminated bucket is visible as such.
    #: Coverage-loss faults that mapped onto a netlist instance.
    mapped_count: int = 0
    #: Coverage-loss faults that did not map. Their connectivity is UNKNOWN.
    unmapped_count: int = 0
    #: Scan status from pin evidence: ``scan`` / ``non_scan`` / ``unknown``.
    scan_evidence_counts: Dict[str, int] = field(default_factory=dict)
    #: Faults whose site resolves to a hard constant: expected, non-actionable.
    tied_constant_count: int = 0
    #: Coverage loss left once tied constants and unmapped rows are removed.
    actionable_loss_count: int = 0
    #: Why the unmapped faults are unmapped (see ``analysis.unresolved``).
    unresolved_causes: Dict[str, int] = field(default_factory=dict)


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
    #: Fault-class breakdown derived from the fault list alone
    #: (``analysis.statistics.DerivedStatistics``). Always the TOTAL
    #: population, so the census reconciles against it.
    statistics: Any = None
    #: The same breakdown with the fault-disposition waiver subclass removed,
    #: i.e. the ATPG tool's "total relevant" column
    #: (``analysis.statistics.DerivedStatistics``). ``None`` when the fault
    #: list holds no waiver subclass, in which case there is one population.
    relevant_statistics: Any = None
    #: Which snapshot of the fault population was analysed and how we know
    #: (``analysis.disposition.DispositionState``). ``None`` for older reports.
    disposition: Any = None
    #: What the fault list declared about itself: format, compression,
    #: version, fault models and whether the faults are collapsed
    #: (``parser.fault_parser.FaultListHeader``). ``None`` for older reports.
    fault_list_header: Any = None
    #: Per-token accounting of fault classes the tool did not recognise
    #: (``diagnostics.UnrecognisedReport``). ``None`` for older reports.
    class_diagnostics: Any = None
    #: What the constraint file actually gave us: how many directives were
    #: recognised, how many could not be evaluated, and which directive names
    #: are unknown (``parser.constraint_parser.ConstraintParseResult.summary``).
    #: Distinguishes "no constraint affects this fault" from "the constraint
    #: file was only partly parsed".
    constraint_diagnostics: Any = None
    #: The configurable conventions this run used
    #: (``config.analysis_config.AnalysisConfig.documented_patterns``).
    analysis_config: Any = None
    #: Coverage-loss categories picked for investigation
    #: (``List[analysis.statistics.SelectedCategory]``).
    selected_categories: Any = None
    #: Ranked fix proposals for those categories
    #: (``List[analysis.recommend.Recommendation]``).
    recommendations: Any = None
    #: Why coverage-loss faults failed to map onto the netlist
    #: (``analysis.unresolved.UnresolvedDiagnosis``). ``None`` when the report
    #: predates the diagnosis or nothing failed to map.
    unresolved_diagnosis: Any = None
    #: Per-category fault files written alongside this report
    #: (``List[reporting.category_dump.CategoryDump]``). Their names are
    #: relative to the file they were written beside, so they only resolve
    #: from that location.
    category_dumps: Any = None
    #: How to reopen this design in the vendor viewer: the launch profile and
    #: the design paths the user supplied (``dict``). ``None`` when no viewer
    #: session has been configured.
    visualizer_config: Any = None
