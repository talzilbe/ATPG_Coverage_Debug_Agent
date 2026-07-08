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

logger = logging.getLogger(__name__)

# Heuristic naming conventions for scan / clock / reset / test-enable signals.
_SCAN_NAME = re.compile(r"(scan|_sff|sdff|_se\b|scan_en|_si\b|_so\b)", re.I)
_NON_SCAN_NAME = re.compile(r"(_nsff|nonscan|non_scan|_dff_|latch|_lat\b)", re.I)
_CLOCK_NAME = re.compile(r"(clk|clock|^ck$|_ck$)", re.I)
_RESET_NAME = re.compile(r"(rst|reset|_rn$|_sn$|clr|clear)", re.I)
_TEST_EN_NAME = re.compile(r"(test_?en|scan_?en|tmode|test_mode|_te\b|_se\b)", re.I)
_CONST_CELL = re.compile(r"(tie|tlo|thi|tieh|tiel|const|logic0|logic1)", re.I)
_SCAN_CELL = re.compile(r"(sdff|sff|scan|muxdff|sdf)", re.I)


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
        for mod_name, module in self.conn.netlist.modules.items():
            if mapping.instance_name in module.instances:
                return mod_name, module.instances[mapping.instance_name]
        return None, None

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
        """Detect a scan/non-scan boundary in the immediate neighbourhood."""
        if inst is None or module is None:
            return False
        this_scan = self._is_scan_cell(inst)
        neighbours = (self.conn.immediate_fan_in(module, inst.name)
                      + self.conn.immediate_fan_out(module, inst.name))
        mixed = False
        for nb in neighbours:
            nb_inst = self.conn.find_instance(module, nb)
            if nb_inst is None:
                continue
            if self._is_scan_cell(nb_inst) != this_scan:
                mixed = True
                result.observed_facts.append(
                    f"Neighbour '{nb}' ({nb_inst.cell_type}) is "
                    f"{'scan' if not this_scan else 'non-scan'} while this "
                    f"cell is {'scan' if this_scan else 'non-scan'}."
                )
                break
        return mixed

    @staticmethod
    def _is_scan_cell(inst: Instance) -> bool:
        if _SCAN_CELL.search(inst.cell_type):
            return True
        if _NON_SCAN_NAME.search(inst.cell_type) or _NON_SCAN_NAME.search(inst.name):
            return False
        if _SCAN_NAME.search(inst.name):
            return True
        return False

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
            return RootCause.TIED_OR_CONSTANT
        if fault.fault_class is FaultClass.TI:
            return RootCause.TIED_OR_CONSTANT

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
                "Confirm the tie/constant is intended; tied nodes are "
                "untestable by design and may be safely waived.",
            RootCause.CLOCK_RESET_TE_BLOCKING:
                "Verify clock/reset/test-enable setup in the ATPG procedure "
                "and constraints.",
            RootCause.STRUCTURAL_MASKING:
                "Examine reconvergent fan-in for masking; consider control or "
                "observe test points.",
            RootCause.UNRESOLVED_CONNECTIVITY:
                "Improve fault-to-netlist mapping (provide full hierarchy) "
                "before drawing conclusions.",
            RootCause.OTHER_STRUCTURAL:
                "Manually inspect the cone of logic around this node.",
        }
        return recs.get(root_cause, "Manually inspect the affected logic.")
