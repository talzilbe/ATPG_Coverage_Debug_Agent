"""Tessent fault-subclass taxonomy.

A Tessent fault list records a *dotted* class token such as ``AU.TC`` or
``UO.AAB``. The suffix is the tool's own root-cause label, which makes it the
single most accurate signal available from the fault list alone — far stronger
than anything that can be inferred structurally from the netlist.

Each :class:`SubclassInfo` entry states what the subclass means, the causes
that produce it, what evidence would confirm or refute the diagnosis, and which
fix actions (see :mod:`..knowledge.fixes`) apply.

Everything in this module is static knowledge: no file is read and no design is
inspected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Fault classes that never represent coverage loss and are therefore excluded
#: from triage (mirrors the "ignore DS/DI/RE" rule used during manual debug).
DETECTED_FAMILIES = ("DS", "DI", "RE", "TI")


@dataclass(frozen=True)
class SubclassInfo:
    """What a single dotted fault subclass means and how to act on it.

    Attributes:
        subclass_id: Canonical dotted id, e.g. ``AU.TC``. A bare family id such
            as ``UC`` is used for faults whose class token carried no subtype.
        title: Short human-readable name.
        family: Coarse fault class (``AU`` / ``UC`` / ``UO`` / ...).
        meaning: One-paragraph explanation of why ATPG assigns this subclass.
        primary_causes: Ordered list of the usual causes, most common first.
        evidence_needed: Observations that confirm or refute the diagnosis.
            Entries prefixed ``[offline]`` are obtainable from the netlist /
            fault list / constraint file; ``[live]`` entries need the ATPG tool.
        fix_ids: Ids into :data:`..knowledge.fixes.FIX_CATALOG`, best first.
        auto_waive_hint: Non-empty when some faults in this subclass are
            routinely *expected* loss that an analyst may legitimately waive.
        caveat: A correction to a common misreading of this subclass.
    """

    subclass_id: str
    title: str
    family: str
    meaning: str
    primary_causes: List[str] = field(default_factory=list)
    evidence_needed: List[str] = field(default_factory=list)
    fix_ids: List[str] = field(default_factory=list)
    auto_waive_hint: str = ""
    caveat: str = ""


def _entry(info: SubclassInfo) -> tuple:
    return info.subclass_id, info


SUBCLASS_CATALOG: Dict[str, SubclassInfo] = dict([
    _entry(SubclassInfo(
        subclass_id="AU.PC",
        title="Pin constraints",
        family="AU",
        meaning=(
            "The fault is blocked by a value held on one or more constrained "
            "pins. Two very different situations share this label: pins the "
            "user deliberately fixed to C0/C1, and pins masked to CX by the "
            "tool — the latter usually meaning the fault is expected to be "
            "covered by a different partition's pattern set."
        ),
        primary_causes=[
            "User-applied C0/C1 constraints on specific named pins.",
            "CX masking in internal mode, i.e. a cross-partition dependency.",
            "An over-broad constraint policy applied at the top level.",
        ],
        evidence_needed=[
            "[offline] Constrained pins present in the fan-in cone of the "
            "faults, and whether few named pins or many pins dominate.",
            "[offline] Constraint kind and value (C0 / C1 / CX) from the "
            "constraint file.",
            "[live] An unwrapped-mode what-if run showing whether coverage "
            "returns once CX is lifted.",
        ],
        fix_ids=["pc_unwrapped_whatif", "pc_relax_to_ct", "pc_waive_named"],
        auto_waive_hint=(
            "Faults blocked solely by explicitly named C0/C1 pins are "
            "user-configured by design and are normally waived rather than "
            "fixed."
        ),
        caveat=(
            "Do not report the whole AU.PC population as recoverable loss. "
            "The CX share is typically covered by another partition."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="AU.TC",
        title="Tied cells",
        family="AU",
        meaning=(
            "A constant value reaches the fault site, so the fault can never "
            "be activated. The constant may come from a tie cell, a test data "
            "register output, an unscanned flop, or an unconstrained input."
        ),
        primary_causes=[
            "A test data register (TDR) output holding a fixed value.",
            "A hardwired tie cell in the fan-in cone.",
            "A non-scan flop driving the cone with a constant.",
            "A primary input left unconstrained at a constant value.",
        ],
        evidence_needed=[
            "[offline] The nearest constant driver reached by tracing fan-in "
            "from the fault site, and how many faults share it.",
            "[offline] Whether that driver looks like a TDR output "
            "(configurable per run) or a hardwired tie (needs RTL change).",
            "[live] analyze_fault output naming the tie source.",
        ],
        fix_ids=["tc_tdr_topoff", "tc_input_constraint", "tc_rtl_change"],
        caveat=(
            "A tie source that is a TDR is cheap to fix with a topoff run; a "
            "hardwired tie is not fixable without an RTL change. Establish "
            "which one it is before recommending anything."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="AU.SEQ",
        title="Sequential depth",
        family="AU",
        meaning=(
            "The fault effect needs more clock cycles to propagate from the "
            "fault site to an observable point than ATPG is allowed to "
            "explore, so it is abandoned as untestable."
        ),
        primary_causes=[
            "Non-scan sequential elements — often memories — in the path.",
            "Missing or incorrect clock definitions, or clock "
            "controllability problems.",
            "Sequential logic complexity or scan isolation blocking a "
            "short-cycle path.",
            "Restrictive constraints preventing the needed state transition.",
            "A sequential-depth setting below what the path requires.",
        ],
        evidence_needed=[
            "[offline] Count of sequential elements on the shortest path from "
            "the fault site to a scan cell or primary output.",
            "[offline] Whether the dominant hierarchy prefix contains memories "
            "or known non-scan structures.",
            "[live] A14/A15/A16 DRC violations, which explain the "
            "classification directly.",
        ],
        fix_ids=["seq_drc_check", "seq_observe_point"],
        caveat=(
            "Raising the sequential-depth limit is rarely the answer — the "
            "standard flow already sets it to the practical maximum of 5. "
            "Look for a structural blocker first."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="AU.BB",
        title="Black box",
        family="AU",
        meaning=(
            "The fault sits inside, or is blocked by, a module that ATPG has "
            "no model for. With no model there is no way to justify or "
            "propagate a value through it."
        ),
        primary_causes=[
            "A module instantiated without an ATPG model.",
            "A macro or hard IP whose model was not read in.",
            "An intentionally black-boxed analog or custom block.",
        ],
        evidence_needed=[
            "[offline] Instances in the fault cone whose module has no "
            "definition in the parsed netlist.",
            "[offline] Which modules dominate the AU.BB population.",
            "[live] report_black_boxes output confirming the boundary.",
        ],
        fix_ids=["bb_add_model", "bb_confirm_boundary"],
        auto_waive_hint=(
            "Loss inside a deliberately black-boxed analog or custom block is "
            "expected and is normally waived."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="AU.UDN",
        title="Undriven nets",
        family="AU",
        meaning=(
            "The fault site sits on a net with no resolvable driver, or on a "
            "branch disconnected in the active mode, so no value can be "
            "justified onto it."
        ),
        primary_causes=[
            "A missing hookup at a module or interface boundary.",
            "A branch gated off in the current mode.",
            "A model gap leaving the driver unresolved.",
        ],
        evidence_needed=[
            "[offline] Fault nets with zero drivers in the parsed netlist.",
            "[offline] Recurring undriven patterns grouped by module or "
            "interface boundary.",
            "[offline] Whether any valid observation path exists downstream.",
        ],
        fix_ids=["udn_trace_hookup"],
    )),
    _entry(SubclassInfo(
        subclass_id="AU.CC",
        title="Cell constraints",
        family="AU",
        meaning=(
            "A cell constraint holds a scan cell at a fixed value, which "
            "prevents ATPG from activating the fault."
        ),
        primary_causes=[
            "Cell constraints applied to stabilise DRC or protect logic.",
            "Constraints left in place from an earlier debug iteration.",
        ],
        evidence_needed=[
            "[offline] Cell constraints in the constraint file that fall in "
            "the fan-in cone of the affected faults.",
        ],
        fix_ids=["cc_review_constraints"],
    )),
    _entry(SubclassInfo(
        subclass_id="AU.MPO",
        title="Masked primary outputs",
        family="AU",
        meaning=(
            "The only observation path for the fault ends at a primary output "
            "that is masked, so the fault effect cannot be captured."
        ),
        primary_causes=[
            "Output masking applied for pattern-size or safety reasons.",
            "An observation path that terminates only at masked outputs.",
        ],
        evidence_needed=[
            "[offline] Whether the fault's fan-out reaches any scan cell at "
            "all, or only primary outputs.",
        ],
        fix_ids=["generic_review"],
    )),
    _entry(SubclassInfo(
        subclass_id="UC.AAB",
        title="Aborted — controllability",
        family="UC",
        meaning=(
            "The ATPG search was abandoned before it could justify the value "
            "needed to activate the fault. The fault is not proven untestable "
            "— the search simply ran out of budget."
        ),
        primary_causes=[
            "Low controllability: the activating value cannot be justified.",
            "Contradictory requirements causing repeated backtracking.",
            "A deep sequential chain feeding the fault site.",
        ],
        evidence_needed=[
            "[offline] Whether the fan-in cone is reachable from a scan cell "
            "or primary input without passing a blocking constraint.",
            "[offline] Cluster concentration and SA0/SA1 asymmetry — a strong "
            "skew suggests a fixed upstream value.",
            "[live] analyze_fault reporting that the fault cannot be "
            "activated.",
        ],
        fix_ids=["aab_control_cutpoint", "aab_abort_limit"],
    )),
    _entry(SubclassInfo(
        subclass_id="UO.AAB",
        title="Aborted — observability",
        family="UO",
        meaning=(
            "The fault could be activated, but the search to propagate its "
            "effect to an observation point was abandoned. As with UC.AAB the "
            "fault is not proven untestable."
        ),
        primary_causes=[
            "Low observability: no reachable observation path.",
            "An observability bottleneck through a single narrow path.",
            "Reconvergent complexity: many observation paths exist but all "
            "require holding numerous set/reset/clock lines simultaneously.",
            "Sequential depth explosion on the propagation path.",
        ],
        evidence_needed=[
            "[offline] Number of distinct fan-out paths reaching a scan cell "
            "or primary output.",
            "[offline] Reconvergence in the fan-out cone, which distinguishes "
            "a true observability gap from search complexity.",
            "[live] analyze_fault observation-point count and detection-path "
            "problem counts.",
        ],
        fix_ids=["aab_observe_cutpoint", "aab_abort_limit",
                 "aab_design_bypass"],
        caveat=(
            "Reconvergent complexity and a true observability gap need "
            "opposite fixes. Raising the abort limit helps the latter and "
            "wastes runtime on the former, so separate them before acting."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="UC.EAB",
        title="EDT aborted — controllability",
        family="UC",
        meaning=(
            "The EDT compression logic could not encode a pattern that "
            "delivers the required care bits, so the fault is dropped."
        ),
        primary_causes=[
            "Too many specified scan cells for the available compression.",
            "Linear dependencies in the EDT encoding.",
            "An unfavourable scan-chain to channel ratio.",
            "An abort limit set too low.",
        ],
        evidence_needed=[
            "[live] report_environment showing the current abort limit.",
            "[live] report_edt_abort_analysis output after pattern creation.",
        ],
        fix_ids=["eab_abort_analysis", "eab_abort_limit"],
        caveat=(
            "EAB is an encoding limitation, not a design defect. Do not "
            "recommend test-point insertion for it."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="UO.EAB",
        title="EDT aborted — observability",
        family="UO",
        meaning=(
            "The EDT compression logic could not encode a pattern that "
            "observes the fault effect, so the fault is dropped."
        ),
        primary_causes=[
            "Too many specified scan cells for the available compression.",
            "Linear dependencies in the EDT encoding.",
            "An unfavourable scan-chain to channel ratio.",
            "An abort limit set too low.",
        ],
        evidence_needed=[
            "[live] report_environment showing the current abort limit.",
            "[live] report_edt_abort_analysis output after pattern creation.",
        ],
        fix_ids=["eab_abort_analysis", "eab_abort_limit"],
        caveat=(
            "EAB is an encoding limitation, not a design defect. Do not "
            "recommend test-point insertion for it."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="AU",
        title="ATPG untestable (unclassified)",
        family="AU",
        meaning=(
            "ATPG proved it cannot generate a pattern for this fault, but the "
            "fault list carries no subtype explaining why."
        ),
        primary_causes=[
            "A constraint, tie, black box or depth limit that was not "
            "attributed to a specific subclass.",
        ],
        evidence_needed=[
            "[offline] Structural analysis of the fault cone.",
            "[offline] Hierarchy clustering to find where the loss "
            "concentrates.",
        ],
        fix_ids=["generic_review"],
        caveat=(
            "Without a subtype the diagnosis rests on structural inference "
            "alone, so state the reduced confidence explicitly."
        ),
    )),
    _entry(SubclassInfo(
        subclass_id="UC",
        title="Uncontrolled (unclassified)",
        family="UC",
        meaning=(
            "ATPG could not establish the value required to activate the "
            "fault, with no subtype recorded."
        ),
        primary_causes=[
            "A blocked or constrained fan-in cone.",
            "An unscanned or tied upstream element.",
        ],
        evidence_needed=[
            "[offline] Constraints and constant drivers in the fan-in cone.",
        ],
        fix_ids=["aab_control_cutpoint", "generic_review"],
    )),
    _entry(SubclassInfo(
        subclass_id="UO",
        title="Unobserved (unclassified)",
        family="UO",
        meaning=(
            "ATPG could not propagate the fault effect to any observation "
            "point, with no subtype recorded."
        ),
        primary_causes=[
            "No scan cell or primary output reachable downstream.",
            "A non-scan element blocking the propagation path.",
        ],
        evidence_needed=[
            "[offline] Fan-out reachability to scan cells or primary outputs.",
        ],
        fix_ids=["aab_observe_cutpoint", "generic_review"],
    )),
])


def describe_subclass(dotted_class: str) -> Optional[SubclassInfo]:
    """Return catalogue knowledge for *dotted_class*, or ``None`` if unknown.

    Lookup is exact first (``AU.TC``), then falls back to the coarse family
    (``AU``). This mirrors the routing precedence used during manual debug:
    exact subclass, then family, then generic handling.

    Args:
        dotted_class: A class id such as ``AU.TC``, ``UO.AAB`` or ``UC``.

    Returns:
        The matching :class:`SubclassInfo`, or ``None`` when neither the exact
        id nor its family is catalogued.
    """
    key = (dotted_class or "").strip().upper()
    if not key:
        return None
    info = SUBCLASS_CATALOG.get(key)
    if info is not None:
        return info
    return SUBCLASS_CATALOG.get(key.split(".", 1)[0])


def is_coverage_loss_class(dotted_class: str) -> bool:
    """True when *dotted_class* represents coverage loss worth debugging.

    Detected families (``DS``, ``DI``, ``RE``) and tied faults (``TI``) are
    excluded — they are never debug targets — and so is ``UNKNOWN``, which
    means the class token was not recognised rather than that coverage was lost.
    """
    family = (dotted_class or "").strip().upper().split(".", 1)[0]
    if not family or family == "UNKNOWN":
        return False
    return family not in DETECTED_FAMILIES
