"""Attribute coverage-loss faults to the structure that blocks them.

A Tessent ``report_statistics`` listing breaks its ``PC`` and ``TC`` rows down
into named contributors: which pin is held at which value, which register is
tying a cone to a constant. That breakdown is what makes those categories
actionable, and it is the difference between "42,000 tied-cell faults" and
"one test data register is holding 92% of them at 1".

This module reconstructs the same breakdown from the netlist and constraint
file by tracing fan-in cones, so it needs no report from the tool. The result
is necessarily an *estimate*: cone tracing cannot reason about Boolean
conditions, multi-driver resolution or mode-dependent gating the way ATPG
does. Everything produced here is therefore labelled as structural inference
and must never be presented with the authority of a tool report.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..models import EvidenceSource, MappingConfidence

logger = logging.getLogger(__name__)

#: How many levels of fan-in to search before giving up on a fault.
DEFAULT_MAX_DEPTH = 6

#: Representative fault paths kept per contributor, quoted verbatim.
DEFAULT_SAMPLES = 3

#: Above this many distinct constrained pins, the blocking set is treated as
#: diffuse rather than as a few deliberately named pins.
NAMED_PIN_LIMIT = 3

#: Share of attributed faults blocked by a masked (X) constraint above which
#: the loss is treated as masking rather than a deliberate fixed value.
MASKED_SHARE = 0.5

# Cell-type and instance-name conventions.
#
# Classification keys off the CELL TYPE, never the instance name alone: an
# instance called ``u_after_tdr`` is usually the gate downstream of a test data
# register, not the register, and an ``AND2`` named ``u_after_tie`` is not a
# tie cell. Instance names only refine a cell that already looks right.
_CONST_CELL = re.compile(r"(tie|tlo|thi|tieh|tiel|const|logic0|logic1)", re.I)
_TIE_HIGH = re.compile(r"(tieh|tiehi|tie1|thi|logic1|const1|_hi\b)", re.I)
_TIE_LOW = re.compile(r"(tiel|tielo|tie0|tlo|logic0|const0|_lo\b)", re.I)
_TDR_NAME = re.compile(r"(tdr|test_data_reg)", re.I)
_SEQ_CELL = re.compile(r"(dff|flop|latch|_reg\b|sdff|sff)", re.I)
_SCAN_CELL = re.compile(r"(sdff|sff|scan|muxdff|sdf)", re.I)
_NON_SCAN_CELL = re.compile(r"(_nsff|nonscan|non_scan|_lat\b)", re.I)


def _is_scan_cell(cell_type: str) -> bool:
    """True when *cell_type* is a scannable sequential cell.

    An explicit non-scan marker wins: ``DFF_nsff`` contains ``sff`` as a
    substring but is precisely the opposite of a scan cell.
    """
    if _NON_SCAN_CELL.search(cell_type):
        return False
    return bool(_SCAN_CELL.search(cell_type))


@dataclass
class TieSource:
    """A constant driver reached from one or more fault sites.

    Attributes:
        driver: Instance name of the constant source.
        cell_type: Its cell type, as parsed from the netlist.
        tie_value: ``'0'`` / ``'1'`` when the value is inferable from the cell
            type, otherwise ``None``.
        kind: ``tie_cell``, ``test_data_register`` or ``non_scan_flop`` — this
            determines whether a fix is cheap, configurable or needs RTL work.
        count: Faults traced back to this driver.
        samples: Representative fault paths, verbatim.
    """

    driver: str
    cell_type: str = ""
    tie_value: Optional[str] = None
    kind: str = "tie_cell"
    count: int = 0
    samples: List[str] = field(default_factory=list)

    @property
    def is_configurable(self) -> bool:
        """True when the source can be reprogrammed rather than redesigned."""
        return self.kind == "test_data_register"

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "driver": self.driver,
            "cell_type": self.cell_type,
            "tie_value": self.tie_value,
            "kind": self.kind,
            "count": self.count,
            "configurable": self.is_configurable,
            "samples": list(self.samples),
        }


@dataclass
class ConstraintSource:
    """A constrained signal reached from one or more fault sites.

    Attributes:
        signal: The constrained signal, as written in the constraint file.
        kind: Constraint kind, e.g. ``constrain`` or ``force``.
        value: ``'0'``, ``'1'`` or ``'X'``.
        count: Faults whose fan-in cone reaches this constraint.
        samples: Representative fault paths, verbatim.
    """

    signal: str
    kind: str = ""
    value: str = ""
    count: int = 0
    samples: List[str] = field(default_factory=list)

    @property
    def is_masked(self) -> bool:
        """True for an X constraint, i.e. a masked rather than fixed value."""
        return (self.value or "").upper() == "X"

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "signal": self.signal,
            "kind": self.kind,
            "value": self.value,
            "masked": self.is_masked,
            "count": self.count,
            "samples": list(self.samples),
        }


@dataclass
class Attribution:
    """What was found to be blocking one coverage-loss category.

    Attributes:
        subclass_id: The category analysed.
        analysed: Faults examined.
        attributed: Faults traced to at least one blocking structure.
        unresolved_mapping: Faults skipped because they never mapped onto a
            netlist object in the first place.
        tie_sources: Constant drivers found, largest contributor first.
        constraint_sources: Constrained signals found, largest first.
        verdict: A short conclusion id, see :meth:`verdict_text`.
        preferred_fix_ids: Fixes the evidence specifically points at.
        note: Human-readable summary of what the tracing established.
    """

    subclass_id: str
    analysed: int = 0
    attributed: int = 0
    unresolved_mapping: int = 0
    tie_sources: List[TieSource] = field(default_factory=list)
    constraint_sources: List[ConstraintSource] = field(default_factory=list)
    verdict: str = "inconclusive"
    preferred_fix_ids: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def coverage(self) -> float:
        """Share of analysed faults that were attributed, 0.0 to 1.0."""
        if not self.analysed:
            return 0.0
        return self.attributed / self.analysed

    @property
    def top_tie(self) -> Optional[TieSource]:
        return self.tie_sources[0] if self.tie_sources else None

    def as_dict(self, limit: int = 5) -> Dict[str, Any]:
        """Plain-dict view for serialisation and tool responses."""
        return {
            "subclass": self.subclass_id,
            "analysed": self.analysed,
            "attributed": self.attributed,
            "unresolved_mapping": self.unresolved_mapping,
            "attribution_rate": round(self.coverage, 4),
            "verdict": self.verdict,
            "note": self.note,
            "source": EvidenceSource.STRUCTURAL_INFERENCE.value,
            "caveat": ("Derived by tracing fan-in cones through the netlist. "
                       "This is an estimate, not the ATPG tool's own "
                       "attribution."),
            "tie_sources": [s.as_dict() for s in self.tie_sources[:limit]],
            "constraint_sources": [s.as_dict()
                                   for s in self.constraint_sources[:limit]],
        }


def _tie_value_from(cell_type: str, inst_name: str) -> Optional[str]:
    """Infer the tied value from naming conventions, or ``None``.

    The cell type is consulted first and only falls back to the instance name,
    so a mismatch between the two cannot silently invert the answer.
    """
    for text in (cell_type, inst_name):
        if not text:
            continue
        if _TIE_HIGH.search(text):
            return "1"
        if _TIE_LOW.search(text):
            return "0"
    return None


class Attributor:
    """Traces fan-in cones to find what blocks a fault.

    Results are memoised per netlist instance. Coverage-loss faults cluster
    heavily onto shared cones, so the same trace would otherwise be repeated
    thousands of times.
    """

    def __init__(self, connectivity: Any, constraints: Iterable[Any] = (),
                 max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        self.conn = connectivity
        self.max_depth = max_depth
        self._by_name: Dict[str, str] = {}
        self._constraint_index: Dict[str, Any] = {}
        self._tie_cache: Dict[str, Optional[Tuple[str, str, str]]] = {}
        self._constraint_cache: Dict[str, Tuple[str, ...]] = {}
        self._build_indexes(constraints)

    def _build_indexes(self, constraints: Iterable[Any]) -> None:
        """Index instances by bare name and constraints by signal name."""
        for key in getattr(self.conn, "instances", {}):
            # ``module::inst_name`` -> remember the first key per bare name.
            name = key.split("::", 1)[-1]
            self._by_name.setdefault(name, key)

        for record in constraints or ():
            signal = getattr(record, "normalized_signal", None) or \
                getattr(record, "signal", None)
            if not signal:
                continue
            self._constraint_index.setdefault(signal, record)
            leaf = signal.rsplit("/", 1)[-1]
            self._constraint_index.setdefault(leaf, record)

    # -- cone tracing -----------------------------------------------------
    def _instance_key(self, instance_name: Optional[str]) -> Optional[str]:
        if not instance_name:
            return None
        return self._by_name.get(instance_name)

    def _walk_upstream(self, start_key: str) -> List[str]:
        """Return instance keys upstream of *start_key*, nearest first."""
        seen: Set[str] = {start_key}
        frontier = [start_key]
        ordered: List[str] = []
        for _ in range(self.max_depth):
            nxt: List[str] = []
            for key in frontier:
                for parent in self.conn.upstream(key):
                    if parent in seen:
                        continue
                    seen.add(parent)
                    ordered.append(parent)
                    nxt.append(parent)
            if not nxt:
                break
            frontier = nxt
        return ordered

    def find_tie_source(self, instance_name: Optional[str]
                        ) -> Optional[Tuple[str, str, str]]:
        """Find the nearest constant driver upstream of *instance_name*.

        Returns:
            ``(driver_name, cell_type, kind)`` for the nearest constant-like
            source, or ``None`` when the cone contains none within the depth
            bound.
        """
        key = self._instance_key(instance_name)
        if key is None:
            return None
        if key in self._tie_cache:
            return self._tie_cache[key]

        found: Optional[Tuple[str, str, str]] = None
        for parent_key in self._walk_upstream(key):
            inst = self.conn.instances.get(parent_key)
            if inst is None:
                continue
            name, cell = inst.name, inst.cell_type or ""

            if _CONST_CELL.search(cell):
                found = (name, cell, "tie_cell")
                break

            if not _SEQ_CELL.search(cell):
                continue
            if _TDR_NAME.search(name) or _TDR_NAME.search(cell):
                # A sequential cell named like a test data register: its value
                # is set per run, so it is the cheapest thing to change.
                found = (name, cell, "test_data_register")
                break
            if not _is_scan_cell(cell):
                # An unscanned sequential element cannot be controlled by
                # ATPG, so it behaves as a constant source for this cone.
                found = (name, cell, "non_scan_flop")
                break

        self._tie_cache[key] = found
        return found

    def find_constraints(self, instance_name: Optional[str]) -> Tuple[str, ...]:
        """Return constraint signals reachable in the fan-in cone."""
        key = self._instance_key(instance_name)
        if key is None or not self._constraint_index:
            return ()
        if key in self._constraint_cache:
            return self._constraint_cache[key]

        hits: List[str] = []
        seen: Set[str] = set()
        for cone_key in [key] + self._walk_upstream(key):
            inst = self.conn.instances.get(cone_key)
            if inst is None:
                continue
            for pin in inst.pins:
                if not pin.net:
                    continue
                net = pin.net.replace(".", "/").lstrip("/")
                for candidate in (net, net.rsplit("/", 1)[-1]):
                    record = self._constraint_index.get(candidate)
                    if record is None:
                        continue
                    signal = getattr(record, "signal", candidate)
                    if signal not in seen:
                        seen.add(signal)
                        hits.append(signal)

        result = tuple(hits)
        self._constraint_cache[key] = result
        return result

    def constraint_record(self, signal: str) -> Optional[Any]:
        """Return the constraint record for *signal*, if indexed."""
        return self._constraint_index.get(signal) or \
            self._constraint_index.get(signal.rsplit("/", 1)[-1])


def _add_sample(samples: List[str], path: str) -> None:
    if path and len(samples) < DEFAULT_SAMPLES:
        samples.append(path)


def attribute_tie_sources(results: Iterable[Any], attributor: Attributor,
                          subclass_id: str = "AU.TC") -> Attribution:
    """Trace tied-cell faults back to the constant driver holding them.

    Args:
        results: ``FaultAnalysisResult`` objects for one category.
        attributor: A configured :class:`Attributor`.
        subclass_id: The category being attributed.

    Returns:
        An :class:`Attribution` ranking the constant drivers found.
    """
    attribution = Attribution(subclass_id=subclass_id)
    sources: Dict[str, TieSource] = {}

    for result in results:
        attribution.analysed += 1
        mapping = result.mapping
        if mapping.confidence is MappingConfidence.UNRESOLVED:
            attribution.unresolved_mapping += 1
            continue

        found = attributor.find_tie_source(mapping.instance_name)
        if found is None:
            continue
        driver, cell_type, kind = found
        source = sources.get(driver)
        if source is None:
            source = TieSource(
                driver=driver,
                cell_type=cell_type,
                tie_value=_tie_value_from(cell_type, driver),
                kind=kind,
            )
            sources[driver] = source
        source.count += 1
        _add_sample(source.samples, result.fault.fault_object)
        attribution.attributed += 1

    attribution.tie_sources = sorted(
        sources.values(), key=lambda s: (-s.count, s.driver))
    _finalise_tie_verdict(attribution)
    return attribution


def _finalise_tie_verdict(attribution: Attribution) -> None:
    """Set the verdict, preferred fixes and note for a tie attribution."""
    top = attribution.top_tie
    if top is None:
        attribution.verdict = "inconclusive"
        attribution.note = (
            f"No constant driver was found within "
            f"{DEFAULT_MAX_DEPTH} levels of fan-in for any of the "
            f"{attribution.analysed} fault(s) examined."
            if attribution.analysed else "No faults to attribute.")
        if attribution.unresolved_mapping:
            attribution.note += (
                f" {attribution.unresolved_mapping} fault(s) could not be "
                f"mapped onto a netlist object at all.")
        return

    share = top.count / max(attribution.attributed, 1)
    value = f" held at {top.tie_value}" if top.tie_value else ""

    if top.kind == "test_data_register":
        attribution.verdict = "configurable_register"
        attribution.preferred_fix_ids = ["tc_tdr_topoff"]
        attribution.note = (
            f"'{top.driver}'{value} looks like a test data register and is "
            f"upstream of {top.count} attributed fault(s) ({share:.0%}). "
            f"A register is configurable per run, so a topoff with the "
            f"opposite value is the cheap fix to try.")
    elif top.kind == "non_scan_flop":
        attribution.verdict = "non_scan_drive"
        attribution.preferred_fix_ids = ["tc_rtl_change"]
        attribution.note = (
            f"'{top.driver}' ({top.cell_type}) is an unscanned sequential "
            f"element upstream of {top.count} attributed fault(s) "
            f"({share:.0%}). ATPG cannot control it, so recovering these "
            f"faults needs a DFT change rather than a rerun.")
    else:
        attribution.verdict = "hardwired_tie"
        attribution.preferred_fix_ids = ["tc_rtl_change"]
        attribution.note = (
            f"'{top.driver}' ({top.cell_type}){value} is a constant driver "
            f"upstream of {top.count} attributed fault(s) ({share:.0%}). "
            f"A hardwired tie cannot be overridden from the ATPG side.")

    if attribution.coverage < 0.5:
        attribution.note += (
            f" Only {attribution.coverage:.0%} of the faults examined could "
            f"be traced to any source, so treat this as a partial picture.")


def attribute_pin_constraints(results: Iterable[Any], attributor: Attributor,
                              subclass_id: str = "AU.PC") -> Attribution:
    """Trace pin-constraint faults back to the constrained signals.

    Separates the two very different situations that share this category: a
    few pins the user deliberately fixed, versus a diffuse or masked blocking
    set that usually means the faults belong to another partition's patterns.

    Args:
        results: ``FaultAnalysisResult`` objects for one category.
        attributor: A configured :class:`Attributor`.
        subclass_id: The category being attributed.

    Returns:
        An :class:`Attribution` ranking the constrained signals found.
    """
    attribution = Attribution(subclass_id=subclass_id)
    sources: Dict[str, ConstraintSource] = {}
    masked_faults = 0

    for result in results:
        attribution.analysed += 1
        mapping = result.mapping
        if mapping.confidence is MappingConfidence.UNRESOLVED:
            attribution.unresolved_mapping += 1
            continue

        signals = attributor.find_constraints(mapping.instance_name)
        if not signals:
            continue

        any_masked = False
        for signal in signals:
            record = attributor.constraint_record(signal)
            source = sources.get(signal)
            if source is None:
                source = ConstraintSource(
                    signal=signal,
                    kind=getattr(record, "kind", "") if record else "",
                    value=(getattr(record, "value", "") or "") if record else "",
                )
                sources[signal] = source
            source.count += 1
            _add_sample(source.samples, result.fault.fault_object)
            any_masked = any_masked or source.is_masked
        if any_masked:
            masked_faults += 1
        attribution.attributed += 1

    attribution.constraint_sources = sorted(
        sources.values(), key=lambda s: (-s.count, s.signal))
    _finalise_pin_verdict(attribution, masked_faults)
    return attribution


def _finalise_pin_verdict(attribution: Attribution,
                          masked_faults: int) -> None:
    """Set the verdict, preferred fixes and note for a pin attribution."""
    distinct = len(attribution.constraint_sources)
    if not distinct:
        attribution.verdict = "inconclusive"
        attribution.note = (
            f"No constrained signal was found within {DEFAULT_MAX_DEPTH} "
            f"levels of fan-in for any of the {attribution.analysed} fault(s) "
            f"examined." if attribution.analysed else "No faults to attribute.")
        if attribution.unresolved_mapping:
            attribution.note += (
                f" {attribution.unresolved_mapping} fault(s) could not be "
                f"mapped onto a netlist object at all.")
        return

    masked_share = masked_faults / max(attribution.attributed, 1)
    named = ", ".join(f"'{s.signal}'"
                      for s in attribution.constraint_sources[:3])

    if masked_share >= MASKED_SHARE or distinct > NAMED_PIN_LIMIT:
        attribution.verdict = "diffuse_or_masked"
        attribution.preferred_fix_ids = ["pc_unwrapped_whatif"]
        attribution.note = (
            f"{distinct} distinct constrained signal(s) reach these faults "
            f"and {masked_share:.0%} of the attributed faults sit behind a "
            f"masked (X) constraint. A blocking set this broad usually means "
            f"the faults are covered by another partition's patterns rather "
            f"than lost — confirm with an unwrapped-mode run before treating "
            f"it as recoverable loss.")
    else:
        attribution.verdict = "user_configured"
        attribution.preferred_fix_ids = ["pc_waive_named", "pc_relax_to_ct"]
        attribution.note = (
            f"The blocking set is just {distinct} explicitly named signal(s) "
            f"({named}) held at fixed values. That is configured loss rather "
            f"than a defect: either waive it or, if the owner agrees the "
            f"opposite value is safe, recover it with a topoff.")

    if attribution.coverage < 0.5:
        attribution.note += (
            f" Only {attribution.coverage:.0%} of the faults examined could "
            f"be traced to a constraint, so treat this as a partial picture.")


#: Categories this module knows how to attribute, mapped to their tracer.
_ATTRIBUTORS = {
    "AU.TC": attribute_tie_sources,
    "AU.PC": attribute_pin_constraints,
}


def attribute_categories(selected: List[Any], fault_results: Iterable[Any],
                         connectivity: Any,
                         constraints: Iterable[Any] = (),
                         max_depth: int = DEFAULT_MAX_DEPTH) -> List[Any]:
    """Attach an :class:`Attribution` to every category that supports one.

    Only ``AU.TC`` and ``AU.PC`` are attributed: they are the two categories
    whose blocking structure can be located reliably by tracing fan-in, and
    the two where naming the contributor changes what the engineer does next.

    Args:
        selected: ``SelectedCategory`` objects to enrich in place.
        fault_results: All coverage-loss analysis results.
        connectivity: The ``ConnectivityModel`` for the design.
        constraints: Parsed constraint records.
        max_depth: Fan-in levels to search.

    Returns:
        The same list, with ``attribution`` populated where applicable.
    """
    targets = {c.subclass_id for c in selected} & set(_ATTRIBUTORS)
    if not targets or connectivity is None:
        return selected

    grouped: Dict[str, List[Any]] = {key: [] for key in targets}
    for result in fault_results or ():
        key = result.fault.dotted_class
        if key in grouped:
            grouped[key].append(result)

    attributor = Attributor(connectivity, constraints, max_depth=max_depth)
    for category in selected:
        tracer = _ATTRIBUTORS.get(category.subclass_id)
        if tracer is None:
            continue
        category.attribution = tracer(
            grouped.get(category.subclass_id, []), attributor,
            subclass_id=category.subclass_id)
        logger.info("Attributed %s: %d/%d fault(s), verdict=%s",
                    category.subclass_id,
                    category.attribution.attributed,
                    category.attribution.analysed,
                    category.attribution.verdict)
    return selected
