"""Conservative, evidence-based root-cause classification.

The engine assigns one :class:`RootCause` per coverage-loss fault using only
structurally provable facts plus clearly-labelled heuristic inferences. It
never claims certainty: every result separates *observed facts* from *inferred
conclusions* and carries evidence strings.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from ..models import (
    ConstraintRecord,
    FaultAnalysisResult,
    FaultClass,
    FaultRecord,
    Instance,
    MappingConfidence,
    RootCause,
)
from .connectivity import ConnectivityModel
from .mapper import FaultMapper
from ..parser.verilog_parser import scan_pin_role

logger = logging.getLogger(__name__)

# Heuristic naming conventions for scan / clock / reset / test-enable signals.
_SCAN_NAME = re.compile(r"(scan|_sff|sdff|_se\b|scan_en|_si\b|_so\b)", re.I)
_NON_SCAN_NAME = re.compile(r"(_nsff|nonscan|non_scan|_dff_|latch|_lat\b)", re.I)
_CLOCK_NAME = re.compile(r"(clk|clock|^ck$|_ck$)", re.I)
_RESET_NAME = re.compile(r"(rst|reset|_rn$|_sn$|clr|clear)", re.I)
_TEST_EN_NAME = re.compile(r"(test_?en|scan_?en|tmode|test_mode|_te\b|_se\b)", re.I)
_CONST_CELL = re.compile(r"(tie|tlo|thi|tieh|tiel|const|logic0|logic1)", re.I)
_SCAN_CELL = re.compile(r"(sdff|sff|scan|muxdff|sdf)", re.I)

#: Pin names treated as the data/enable inputs of a cell. A hard constant on
#: one of these is what makes a stuck-at fault at the site undetectable, so
#: they are the pins whose real driver must be resolved before any
#: scan-boundary or observability story is told.
_DATA_ENABLE_PIN = re.compile(
    r"^(d|di|d\d+|data|den|de|e|en|ena|enable|ce|le|g|gate)$", re.I)

#: Clock and reset pins, excluded from the data/enable search.
_CLOCK_PIN = re.compile(r"^(clk|ck|clock|cp|gclk|clkin)$", re.I)
_RESET_PIN = re.compile(r"^(r|rb|rn|rst|reset|clr|cd|sb|sn|set)$", re.I)


class RootCauseEngine:
    """Classifies coverage-loss faults into root causes with evidence."""

    def __init__(self, connectivity: ConnectivityModel, mapper: FaultMapper,
                 constraints: List[ConstraintRecord]) -> None:
        self.conn = connectivity
        self.mapper = mapper
        self.constraints = constraints
        # Index constraints by normalised signal for quick lookup.
        self._constraint_index: Dict[str, List[ConstraintRecord]] = {}
        for c in constraints:
            if c.normalized_signal:
                self._constraint_index.setdefault(
                    c.normalized_signal, []).append(c)

    # -- public API -------------------------------------------------------
    def analyze_fault(self, fault: FaultRecord) -> FaultAnalysisResult:
        """Produce a full :class:`FaultAnalysisResult` for one fault."""
        mapping = self.mapper.map_object(fault.fault_object)
        result = FaultAnalysisResult(fault=fault, mapping=mapping)
        result.evidence.extend(mapping.evidence)

        module, inst = self._locate(mapping)
        if inst is not None and module is not None:
            result.fan_in = self.conn.immediate_fan_in(module, inst.name)
            result.fan_out = self.conn.immediate_fan_out(module, inst.name)
            result.observed_facts.append(
                f"Instance '{inst.name}' ({inst.cell_type}) has "
                f"{len(result.fan_in)} fan-in and {len(result.fan_out)} "
                f"fan-out instance(s)."
            )
            if inst.source_text:
                result.observed_facts.append(
                    f"Instantiation (netlist line {inst.line_number}): "
                    f"{inst.source_text}"
                )
                result.scan_evidence = inst.source_text
            scan = self._is_scan_cell(inst)
            result.scan_cell_state = (
                "unknown" if scan is None else ("scan" if scan else "non_scan"))
            result.observed_facts.append(
                f"Scan status from the pin list: {result.scan_cell_state}."
            )
            result.driver_resolution = self._resolve_site_driver(
                module, inst, mapping.pin_name)
            if result.driver_resolution is not None:
                res = result.driver_resolution
                result.observed_facts.append(
                    f"Driver of the fault site resolved across "
                    f"{res.levels} hierarchy hop(s) to {res.describe()}."
                )
                result.observed_facts.extend(
                    f"  hop: {hop}" for hop in res.trace)
                if res.is_tie:
                    result.tie_driver = {
                        "instance": res.instance.name,
                        "cell_type": res.instance.cell_type,
                        "value": res.tie_value,
                        "net": res.net,
                        "levels": res.levels,
                        "trace": list(res.trace),
                    }
        else:
            result.observed_facts.append(
                "This fault object did not map onto a netlist instance. No "
                "connectivity was measured: fan-in, fan-out and scan status "
                "are UNKNOWN for this row, not zero and not 'no'."
            )

        self._flag_controllability_observability(fault, result)
        constraint_hits = self._constraint_hits(mapping, module, inst)
        if constraint_hits:
            result.constraint_related = True
            for c in constraint_hits:
                result.observed_facts.append(
                    f"Constraint (line {c.line_number}, kind={c.kind}) on "
                    f"signal '{c.signal}'."
                )
        scan_boundary = self._scan_boundary(module, inst, result)
        result.scan_boundary_involved = scan_boundary

        result.root_cause = self._classify(
            fault, result, mapping, inst, constraint_hits, scan_boundary
        )
        result.recommended_step = self._recommend(result.root_cause, result)
        return result

    # -- helpers ----------------------------------------------------------
    def _locate(self, mapping) -> Tuple[Optional[str], Optional[Instance]]:
        if mapping.confidence is MappingConfidence.UNRESOLVED:
            return None, None
        module = getattr(mapping, "module_name", None)
        if module and module in self.conn.netlist.modules:
            inst = self.conn.netlist.modules[module].instances.get(
                mapping.instance_name)
            if inst is not None:
                return module, inst
        for mod_name, module_obj in self.conn.netlist.modules.items():
            if mapping.instance_name in module_obj.instances:
                return mod_name, module_obj.instances[mapping.instance_name]
        return None, None

    def _resolve_site_driver(self, module: str, inst: Instance,
                             pin_name: Optional[str]):
        """Resolve the real driver of the fault site across hierarchy.

        Prefers the pin named by the fault object; when the fault names no
        pin, falls back to the cell's data/enable inputs. Clock, reset and
        scan pins are excluded -- a constant there is a different finding and
        must not be reported as a tied data condition.

        Returns:
            A ``DriverResolution`` for the first pin whose driver resolves to
            a tie cell, otherwise the first resolution found, otherwise
            ``None`` (meaning *not resolved*, never *no driver*).
        """
        pins = self._site_pins(inst, pin_name)
        first = None
        for pin in pins:
            if not pin.net:
                continue
            resolution = self.conn.resolve_driver(module, pin.net)
            if resolution is None:
                continue
            if resolution.is_tie:
                return resolution
            if first is None:
                first = resolution
        return first

    @staticmethod
    def _site_pins(inst: Instance, pin_name: Optional[str]) -> List:
        """Pins whose driver decides this fault site."""
        if pin_name:
            for pin in inst.pins:
                if pin.name.lower() == pin_name.lower():
                    if not (_CLOCK_PIN.match(pin.name)
                            or scan_pin_role(pin.name)):
                        return [pin]
                    return []
        return [pin for pin in inst.pins
                if _DATA_ENABLE_PIN.match(pin.name)
                and not _CLOCK_PIN.match(pin.name)
                and not _RESET_PIN.match(pin.name)
                and not scan_pin_role(pin.name)]

    def _flag_controllability_observability(
        self, fault: FaultRecord, result: FaultAnalysisResult
    ) -> None:
        """Map fault class to the affected ATPG dimension (observed fact)."""
        if fault.fault_class is FaultClass.UC:
            result.controllability_issue = True
            result.observed_facts.append(
                "Fault class UC: ATPG could not control this node."
            )
        elif fault.fault_class is FaultClass.UO:
            result.observability_issue = True
            result.observed_facts.append(
                "Fault class UO: ATPG could not observe this node."
            )
        elif fault.fault_class is FaultClass.AU:
            # AU can stem from either dimension; mark both as candidate issues.
            result.controllability_issue = True
            result.observability_issue = True
            result.observed_facts.append(
                "Fault class AU: ATPG-untestable (controllability and/or "
                "observability)."
            )

    def _constraint_hits(self, mapping, module: Optional[str],
                         inst: Optional[Instance]) -> List[ConstraintRecord]:
        """Find constraints touching the fault net or instance nets."""
        hits: List[ConstraintRecord] = []
        targets: Set[str] = set()
        if mapping.matched_net:
            targets.add(mapping.normalized_object)
        if inst is not None:
            for pin in inst.pins:
                if pin.net:
                    targets.add(pin.net.replace(".", "/").lstrip("/"))
        for norm_sig, records in self._constraint_index.items():
            for target in targets:
                if norm_sig and (norm_sig == target
                                 or target.endswith("/" + norm_sig)
                                 or norm_sig.endswith("/" + target)
                                 or norm_sig.split("/")[-1] == target.split("/")[-1]):
                    hits.extend(records)
                    break
        # De-duplicate by line number.
        seen = set()
        unique = []
        for h in hits:
            if h.line_number not in seen:
                seen.add(h.line_number)
                unique.append(h)
        return unique

    def _scan_boundary(self, module: Optional[str], inst: Optional[Instance],
                       result: FaultAnalysisResult) -> bool:
        """Detect a scan/non-scan boundary in the immediate neighbourhood.

        Scan status is taken from pin evidence only. When either side of a
        neighbour pair cannot be decided from its pin list, no boundary is
        claimed -- an undecidable cell is not evidence of non-scan logic.
        """
        if inst is None or module is None:
            return False
        this_scan = self._is_scan_cell(inst)
        if this_scan is None:
            return False
        neighbours = (self.conn.immediate_fan_in(module, inst.name)
                      + self.conn.immediate_fan_out(module, inst.name))
        mixed = False
        for nb in neighbours:
            nb_inst = self.conn.find_instance(module, nb)
            if nb_inst is None:
                continue
            nb_scan = self._is_scan_cell(nb_inst)
            if nb_scan is None or nb_scan == this_scan:
                continue
            mixed = True
            result.observed_facts.append(
                f"Neighbour '{nb}' ({nb_inst.cell_type}) is "
                f"{'scan' if nb_scan else 'non-scan'} while this cell is "
                f"{'scan' if this_scan else 'non-scan'}; both decided from "
                f"their pin lists."
            )
            break
        return mixed

    @staticmethod
    def _is_scan_cell(inst: Instance) -> Optional[bool]:
        """Scan status from the pin list, or ``None`` when undecidable.

        A cell is scan when its instantiation carries both a dedicated
        scan-data input and a shift-enable pin. Instance and cell naming are
        deliberately not consulted: naming was the basis on which this engine
        once declared a flop with ``.si``/``.ssb``/``.so`` to be non-scan.
        """
        if not inst.pins:
            return None
        roles = {scan_pin_role(pin.name) for pin in inst.pins}
        if "scan_in" in roles and "shift_enable" in roles:
            return True
        if "scan_in" not in roles and "shift_enable" not in roles:
            return False
        return None

    def _classify(self, fault: FaultRecord, result: FaultAnalysisResult,
                  mapping, inst: Optional[Instance],
                  constraint_hits: List[ConstraintRecord],
                  scan_boundary: bool) -> RootCause:
        """Apply conservative ordered rules to pick a root cause."""
        # 0. Unresolved connectivity dominates -- we cannot prove anything.
        if mapping.confidence is MappingConfidence.UNRESOLVED:
            result.inferred_conclusions.append(
                "Mapping unresolved; root cause cannot be proven structurally."
            )
            return RootCause.UNRESOLVED_CONNECTIVITY

        # 1. Tied/constant hardware.
        if inst is not None and _CONST_CELL.search(inst.cell_type):
            result.inferred_conclusions.append(
                "Cell type indicates a tie/constant driver."
            )
            return RootCause.TIED_CONSTANT

        # 1b. PRECEDENCE: a resolved tie driver beats every rule below.
        #
        # A stuck-at fault on a pin held at a hard constant is undetectable
        # because no differing value can ever be established there. That is
        # true regardless of scan architecture, so scan-boundary and
        # observability categories must not be used for it. The driver is
        # resolved across hierarchy feedthrough ports, which is why this fires
        # even when the tie cell sits four levels away from the fault site.
        resolution = result.driver_resolution
        if resolution is not None and resolution.is_tie:
            value = (f" holding it at constant {resolution.tie_value}"
                     if resolution.tie_value else "")
            result.inferred_conclusions.append(
                f"The fault site is driven by tie cell "
                f"'{resolution.instance.name}' "
                f"({resolution.instance.cell_type}){value}, reached across "
                f"{resolution.levels} hierarchy hop(s). Expected and "
                f"non-actionable: no differing value can be established, so "
                f"this is not a scan or observability problem."
            )
            return RootCause.TIED_CONSTANT

        if fault.fault_class is FaultClass.TI:
            return RootCause.TIED_CONSTANT

        # 2. Constraint-induced loss (split by controllability/observability).
        if constraint_hits:
            if self._touches_clock_reset_te(constraint_hits, inst):
                result.inferred_conclusions.append(
                    "A clock/reset/test-enable signal is constrained near "
                    "this fault."
                )
                return RootCause.CLOCK_RESET_TE_BLOCKING
            if fault.fault_class is FaultClass.UO:
                result.inferred_conclusions.append(
                    "Constraint plausibly blocks the observation path."
                )
                return RootCause.CONSTRAINT_OBSERVABILITY
            result.inferred_conclusions.append(
                "Constraint plausibly fixes/limits controllability of this "
                "node."
            )
            return RootCause.CONSTRAINT_CONTROLLABILITY

        # 3. Scan/non-scan boundary effects.
        if scan_boundary:
            if fault.fault_class is FaultClass.UO:
                result.inferred_conclusions.append(
                    "Non-scan logic in the observe path likely blocks "
                    "propagation."
                )
                return RootCause.NON_SCAN_PROPAGATION
            result.inferred_conclusions.append(
                "Scannable logic connects to non-scan logic at this boundary."
            )
            return RootCause.SCAN_TO_NON_SCAN

        # 4. Clock/reset/test-enable cell by naming.
        if inst is not None and (_CLOCK_NAME.search(inst.name)
                                 or _RESET_NAME.search(inst.name)
                                 or _TEST_EN_NAME.search(inst.name)):
            result.inferred_conclusions.append(
                "Instance name suggests a clock/reset/test-enable node."
            )
            return RootCause.CLOCK_RESET_TE_BLOCKING

        # 5. Reconvergence / masking heuristic: many fan-ins converging.
        if len(result.fan_in) >= 3 and len(result.fan_out) <= 1:
            result.inferred_conclusions.append(
                "High fan-in with low fan-out suggests structural masking or "
                "reconvergence."
            )
            return RootCause.STRUCTURAL_MASKING

        result.inferred_conclusions.append(
            "No specific structural cause matched; classified as a generic "
            "structural cause."
        )
        return RootCause.OTHER_STRUCTURAL

    def _touches_clock_reset_te(self, constraint_hits: List[ConstraintRecord],
                                inst: Optional[Instance]) -> bool:
        for c in constraint_hits:
            if c.kind in ("clock", "reset", "test_enable"):
                return True
            sig = (c.signal or "")
            if (_CLOCK_NAME.search(sig) or _RESET_NAME.search(sig)
                    or _TEST_EN_NAME.search(sig)):
                return True
        return False

    @staticmethod
    def _recommend(root_cause: RootCause, result: FaultAnalysisResult) -> str:
        """Return a concrete next debug step for the engineer."""
        recs = {
            RootCause.CONSTRAINT_CONTROLLABILITY:
                "Review the listed constraint(s); relax or justify the forced "
                "value to restore controllability.",
            RootCause.CONSTRAINT_OBSERVABILITY:
                "Check whether the constraint blocks the observe path; add an "
                "observe point or relax the constraint.",
            RootCause.SCAN_TO_NON_SCAN:
                "Inspect the scan/non-scan boundary; consider making the "
                "neighbouring flop scannable or adding test points.",
            RootCause.NON_SCAN_PROPAGATION:
                "Non-scan logic blocks propagation; add an observe test point "
                "downstream or convert the blocking flop to scan.",
            RootCause.TIED_OR_CONSTANT:
                "Expected / non-actionable: the site is held at a hard "
                "constant, so no differing value can be established. Confirm "
                "the tie is intended and waive; do not spend debug effort "
                "here and do not treat it as a scan or observability issue.",
            RootCause.CLOCK_RESET_TE_BLOCKING:
                "Verify clock/reset/test-enable setup in the ATPG procedure "
                "and constraints.",
            RootCause.STRUCTURAL_MASKING:
                "Examine reconvergent fan-in for masking; consider control or "
                "observe test points.",
            RootCause.UNRESOLVED_CONNECTIVITY:
                "Mapping failed, so nothing about this site is known -- not "
                "its fan-in, not its fan-out, not its scan status. Re-run "
                "with the full hierarchical netlist and resolve the mapping "
                "before drawing any conclusion.",
            RootCause.OTHER_STRUCTURAL:
                "Manually inspect the cone of logic around this node.",
        }
        return recs.get(root_cause, "Manually inspect the affected logic.")
