"""Catalogue of candidate fixes for ATPG coverage loss.

Each :class:`FixAction` pairs a subclass diagnosis with a concrete next step:
why it applies, what must be true before trying it, the commands to run, and
what result would confirm it worked.

Two rules are enforced by the data model rather than by convention:

* ``requires_measurement`` marks actions whose benefit can only be established
  by re-running ATPG. The reporting layer must never print a predicted coverage
  gain for these — an unmeasured estimate is not evidence.
* ``commands`` are emitted as text for a human to run. This package never
  executes them, so the offline analysis has no tool dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

#: Hard ceiling on the ATPG abort limit. Higher values cause impractical
#: pattern-generation runtimes for negligible additional coverage.
MAX_ABORT_LIMIT = 500

#: The standard flow already sets sequential depth to this value, which is the
#: practical maximum, so "raise the depth" is almost never a valid recommendation.
MAX_PRACTICAL_SEQ_DEPTH = 5


@dataclass(frozen=True)
class FixAction:
    """One candidate remedy for a diagnosed coverage-loss category.

    Attributes:
        fix_id: Stable identifier referenced from
            :data:`..knowledge.subclasses.SUBCLASS_CATALOG`.
        title: Short imperative summary of the action.
        applies_to: Dotted subclass ids this action addresses.
        rationale: Why this action addresses the diagnosed cause.
        preconditions: What must be confirmed before spending time on it.
        commands: Tessent commands to run, as copyable text. May be empty for
            actions that are design or review work rather than tool runs.
        expected_effect: What a successful outcome looks like.
        effort: ``low`` / ``medium`` / ``high`` — analyst time and runtime.
        risk: ``low`` / ``medium`` / ``high`` — chance of side effects.
        requires_measurement: True when the benefit must be measured by a
            re-run rather than estimated.
        feasibility_rank: Ordering hint used when ranking recommendations;
            lower is cheaper. 1 = configuration change, 2 = tool run,
            3 = DFT insertion, 4 = RTL change.
        caveat: A warning that prevents a common misapplication.
    """

    fix_id: str
    title: str
    applies_to: List[str]
    rationale: str
    preconditions: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    expected_effect: str = ""
    effort: str = "medium"
    risk: str = "low"
    requires_measurement: bool = True
    feasibility_rank: int = 2
    caveat: str = ""


def _entry(action: FixAction) -> tuple:
    return action.fix_id, action


FIX_CATALOG: Dict[str, FixAction] = dict([
    # -- AU.PC ------------------------------------------------------------
    _entry(FixAction(
        fix_id="pc_unwrapped_whatif",
        title="Confirm the CX share is a cross-partition dependency",
        applies_to=["AU.PC"],
        rationale=(
            "When many pins rather than a few named ones dominate the "
            "blocking set, the loss is usually CX masking in internal mode. "
            "Re-running in unwrapped mode lifts CX and shows whether the "
            "faults are recovered by another partition's patterns."
        ),
        preconditions=[
            "The dominant AU.PC contribution comes from many pins or from CX "
            "rather than from a small set of explicitly named C0/C1 pins.",
        ],
        commands=[
            "set_system_mode setup -force",
            "set_current_mode -type unwrapped",
            "set_system_mode analysis",
            "read_faults <au_pc_faults_file> -retain",
            "add_processor localhost:8",
            "reset_au_faults",
            "create_patterns",
            "report_statistics -detailed_analysis",
        ],
        expected_effect=(
            "If AU.PC drops sharply, the loss is covered by another partition "
            "and is by design. If it does not move, the cause is not "
            "mode-related — triage the individual C0/C1 constraints instead."
        ),
        effort="medium",
        requires_measurement=True,
        feasibility_rank=2,
    )),
    _entry(FixAction(
        fix_id="pc_relax_to_ct",
        title="Re-apply constraints as CT0/CT1, or top off with the opposite value",
        applies_to=["AU.PC"],
        rationale=(
            "A pin held at a single value blocks every fault needing the "
            "opposite value. Applying the constraint as a test-mode value that "
            "can toggle, or running a topoff pass with the opposite value, "
            "recovers that half of the population."
        ),
        preconditions=[
            "Unwrapped-mode analysis has already ruled out cross-partition CX "
            "masking.",
            "The pin owner confirms the opposite value is safe to apply.",
        ],
        commands=[
            "# Review each dominant constrained pin, then in a topoff run:",
            "add_input_constraints <pin> -C1   ;# or -C0 for the opposite value",
        ],
        expected_effect=(
            "AU.PC faults attributed to the retargeted pins move to a "
            "detected class in the topoff run."
        ),
        effort="medium",
        risk="medium",
        requires_measurement=True,
        feasibility_rank=1,
        caveat=(
            "Constraints usually exist for a reason. Confirm with the owner "
            "before changing a value — an unsafe relaxation can invalidate "
            "the whole pattern set."
        ),
    )),
    _entry(FixAction(
        fix_id="pc_waive_named",
        title="Waive faults blocked only by explicitly constrained pins",
        applies_to=["AU.PC"],
        rationale=(
            "Faults blocked by a pin the user deliberately fixed to C0 or C1 "
            "are configured loss, not a design or flow defect. Waiving them "
            "keeps the remaining report focused on recoverable coverage."
        ),
        preconditions=[
            "The blocking pin is explicitly named in the constraint file.",
            "The constraint is intentional and expected to stay.",
        ],
        commands=[],
        expected_effect=(
            "Reported coverage loss reflects only recoverable faults; the "
            "waived population is documented with its justification."
        ),
        effort="low",
        risk="low",
        requires_measurement=False,
        feasibility_rank=1,
        caveat=(
            "Waiving changes what is reported, not what is tested. Record the "
            "justification so the decision can be reviewed later."
        ),
    )),

    # -- AU.TC ------------------------------------------------------------
    _entry(FixAction(
        fix_id="tc_tdr_topoff",
        title="Top off with the tie-source TDR set to the opposite value",
        applies_to=["AU.TC"],
        rationale=(
            "A test data register output is configurable per run. If it holds "
            "the cone at a constant, a topoff run with the register programmed "
            "to the opposite value activates the blocked faults."
        ),
        preconditions=[
            "The dominant tie source is a test data register, not a hardwired "
            "tie cell or a non-scan flop.",
            "The tie value is known, so the opposite value can be programmed.",
        ],
        commands=[
            "# Program the identified TDR to the opposite value, then:",
            "reset_au_faults",
            "create_patterns",
            "report_statistics -detailed_analysis",
        ],
        expected_effect=(
            "AU.TC faults attributed to that register are activated and move "
            "to a detected class."
        ),
        effort="medium",
        requires_measurement=True,
        feasibility_rank=1,
    )),
    _entry(FixAction(
        fix_id="tc_input_constraint",
        title="Constrain the unconstrained input driving the constant",
        applies_to=["AU.TC"],
        rationale=(
            "When the constant originates at a primary input or test port "
            "with no constraint, forcing the opposite value in a topoff run "
            "releases the cone."
        ),
        preconditions=[
            "The traced tie source is a primary input or test port.",
            "No existing constraint already fixes that input.",
        ],
        commands=[
            "add_input_constraints <input_port> -C1  ;# or -C0",
            "reset_au_faults",
            "create_patterns",
        ],
        expected_effect=(
            "The previously constant cone becomes controllable and its faults "
            "are activated."
        ),
        effort="low",
        requires_measurement=True,
        feasibility_rank=1,
    )),
    _entry(FixAction(
        fix_id="tc_rtl_change",
        title="Escalate a hardwired tie to the design owner",
        applies_to=["AU.TC"],
        rationale=(
            "A constant produced by a tie cell or by unscanned logic cannot "
            "be overridden from the ATPG side. Recovering the faults requires "
            "a design change such as scanning the driving flop or adding a "
            "test-mode bypass."
        ),
        preconditions=[
            "The tie source is a tie cell or a non-scan flop, and no "
            "configurable register is involved.",
        ],
        commands=[],
        expected_effect=(
            "The design owner decides whether the loss is acceptable or "
            "warrants a DFT change in the next revision."
        ),
        effort="high",
        risk="high",
        requires_measurement=False,
        feasibility_rank=4,
    )),

    # -- AU.SEQ -----------------------------------------------------------
    _entry(FixAction(
        fix_id="seq_drc_check",
        title="Check A14/A15/A16 DRC violations before anything else",
        applies_to=["AU.SEQ"],
        rationale=(
            "RAM-related DRC violations frequently explain the whole AU.SEQ "
            "population. Resolving them removes the loss without any test-point "
            "work, so this check comes first."
        ),
        preconditions=[],
        commands=[
            "report_drc_rules A14",
            "report_drc_rules A15",
            "report_drc_rules A16",
        ],
        expected_effect=(
            "Violations found here identify the memories or sequential "
            "structures responsible, and fixing them removes the "
            "classification at its source."
        ),
        effort="low",
        risk="low",
        requires_measurement=False,
        feasibility_rank=2,
    )),
    _entry(FixAction(
        fix_id="seq_observe_point",
        title="Break the sequential chain with an observation point",
        applies_to=["AU.SEQ", "UO.AAB"],
        rationale=(
            "An observation point partway along a deep chain shortens the "
            "propagation distance below the depth limit, which is more "
            "effective than raising the limit itself."
        ),
        preconditions=[
            "DRC violations have been ruled out as the cause.",
            "The affected faults concentrate in an identifiable cone.",
        ],
        commands=[
            "# Validate the idea as a cut-point before requesting insertion:",
            "set_system_mode setup -force",
            "add_primary_outputs <fault_pin_from_dominant_cluster>",
            "set_system_mode analysis",
            "reset_au_faults",
            "create_patterns",
            "report_statistics -detailed_analysis",
        ],
        expected_effect=(
            "AU.SEQ count falls in the cut-point run, which justifies a real "
            "test point at the common parent instance."
        ),
        effort="high",
        requires_measurement=True,
        feasibility_rank=3,
        caveat=(
            f"Do not simply raise the sequential depth: the standard flow "
            f"already uses {MAX_PRACTICAL_SEQ_DEPTH}, the practical maximum."
        ),
    )),

    # -- AU.BB / AU.UDN / AU.CC -------------------------------------------
    _entry(FixAction(
        fix_id="bb_confirm_boundary",
        title="Confirm which modules are black boxes",
        applies_to=["AU.BB"],
        rationale=(
            "Ranking the loss by black-box module separates blocks that are "
            "intentionally unmodelled from ones whose model was simply not "
            "read in."
        ),
        preconditions=[],
        commands=["report_black_boxes"],
        expected_effect=(
            "A ranked list of black-box modules with their fault contribution, "
            "each classified as intentional or as a missing model."
        ),
        effort="low",
        risk="low",
        requires_measurement=False,
        feasibility_rank=2,
    )),
    _entry(FixAction(
        fix_id="bb_add_model",
        title="Supply the missing ATPG model",
        applies_to=["AU.BB"],
        rationale=(
            "A block that was meant to be modelled but was not read in "
            "becomes testable once its model is provided."
        ),
        preconditions=[
            "The module is not an intentional black box.",
            "An ATPG model or library cell description exists.",
        ],
        commands=[],
        expected_effect=(
            "Faults inside and behind the block are reclassified once the "
            "model is present."
        ),
        effort="medium",
        risk="low",
        requires_measurement=True,
        feasibility_rank=3,
    )),
    _entry(FixAction(
        fix_id="udn_trace_hookup",
        title="Trace the undriven net to its missing connection",
        applies_to=["AU.UDN"],
        rationale=(
            "Undriven nets usually cluster at one interface boundary, so a "
            "single missing hookup or mode-gating issue often explains the "
            "whole population."
        ),
        preconditions=[],
        commands=[],
        expected_effect=(
            "The missing connection or mode gate is identified and referred "
            "to the integration owner."
        ),
        effort="medium",
        risk="low",
        requires_measurement=False,
        feasibility_rank=3,
    )),
    _entry(FixAction(
        fix_id="cc_review_constraints",
        title="Review the cell constraints blocking activation",
        applies_to=["AU.CC"],
        rationale=(
            "Cell constraints are often added to settle DRC and then left in "
            "place. Removing ones that are no longer needed restores "
            "controllability."
        ),
        preconditions=[
            "The constraint is not required for DRC or functional safety.",
        ],
        commands=["report_cell_constraints"],
        expected_effect=(
            "Constraints that are no longer needed are removed and the "
            "affected faults become activatable."
        ),
        effort="low",
        risk="medium",
        requires_measurement=True,
        feasibility_rank=1,
    )),

    # -- AAB --------------------------------------------------------------
    _entry(FixAction(
        fix_id="aab_control_cutpoint",
        title="Test a control point with an add_primary_inputs cut-point",
        applies_to=["UC.AAB", "UC"],
        rationale=(
            "When activation is the blocker, making the fault site directly "
            "controllable shows how much coverage a real control point would "
            "recover — before committing to DFT insertion."
        ),
        preconditions=[
            "The evidence points to activation failing, not propagation.",
            "Representative fault pins have been taken verbatim from the "
            "dominant cluster.",
        ],
        commands=[
            "set_system_mode setup -force",
            "set_current_mode -type unwrapped",
            "add_primary_inputs <uc_aab_fault_pin>",
            "set_system_mode analysis",
            "read_faults <baseline_faults_file> -retain",
            "reset_au_faults",
            "create_patterns",
            "report_statistics -detailed_analysis",
            "# For the insertion point to report to the DFT owner:",
            "get_common_parent_instance {<pin_a> <pin_b>}",
        ],
        expected_effect=(
            "A drop in UC.AAB against the baseline justifies a control point "
            "at the common parent instance."
        ),
        effort="high",
        requires_measurement=True,
        feasibility_rank=3,
        caveat=(
            "Use fault pins copied exactly from the fault list. A "
            "reconstructed or abbreviated path will not resolve."
        ),
    )),
    _entry(FixAction(
        fix_id="aab_observe_cutpoint",
        title="Test an observation point with an add_primary_outputs cut-point",
        applies_to=["UO.AAB", "UO"],
        rationale=(
            "When the fault activates but the effect cannot be propagated, "
            "making the site directly observable measures the recovery a real "
            "observation point would deliver."
        ),
        preconditions=[
            "The evidence points to propagation failing, not activation.",
            "Representative fault pins have been taken verbatim from the "
            "dominant cluster.",
        ],
        commands=[
            "set_system_mode setup -force",
            "set_current_mode -type unwrapped",
            "add_primary_outputs <uo_aab_fault_pin>",
            "set_system_mode analysis",
            "read_faults <baseline_faults_file> -retain",
            "reset_au_faults",
            "create_patterns",
            "report_statistics -detailed_analysis",
            "# For the insertion point to report to the DFT owner:",
            "get_common_parent_instance {<pin_a> <pin_b>}",
        ],
        expected_effect=(
            "A drop in UO.AAB against the baseline justifies an observation "
            "point at the common parent instance."
        ),
        effort="high",
        requires_measurement=True,
        feasibility_rank=3,
        caveat=(
            "No change after adding cut-points means the cause is "
            "reconvergent complexity, not an observability gap. Test points "
            "will not help; a design bypass is needed instead."
        ),
    )),
    _entry(FixAction(
        fix_id="aab_abort_limit",
        title="Raise the ATPG abort limit",
        applies_to=["UC.AAB", "UO.AAB"],
        rationale=(
            "Aborted faults are not proven untestable. When only a narrow "
            "search bottleneck is in the way, more search budget converts "
            "some of them directly."
        ),
        preconditions=[
            "The current limit has been read with report_environment.",
            "The evidence indicates a narrow bottleneck rather than broad "
            "reconvergence.",
        ],
        commands=[
            "report_environment",
            f"set_atpg_limits -abort_limit {MAX_ABORT_LIMIT}",
            "create_patterns",
            "report_statistics -detailed_analysis",
        ],
        expected_effect=(
            "A modest reduction in aborted faults at the cost of longer "
            "pattern-generation runtime."
        ),
        effort="low",
        risk="medium",
        requires_measurement=True,
        feasibility_rank=1,
        caveat=(
            f"Never exceed an abort limit of {MAX_ABORT_LIMIT}. Beyond that "
            "runtime grows sharply for negligible coverage, and it does not "
            "help at all when the cause is reconvergent complexity."
        ),
    )),
    _entry(FixAction(
        fix_id="aab_design_bypass",
        title="Request a design bypass for reconvergent structures",
        applies_to=["UO.AAB"],
        rationale=(
            "Structures such as XOR trees, parity and masking logic force "
            "ATPG to satisfy many simultaneous conditions. This is structural: "
            "neither more search budget nor test points will resolve it, so a "
            "test-mode bypass or a dedicated IP test is required."
        ),
        preconditions=[
            "Cut-point testing produced no improvement.",
            "The fan-out cone shows heavy, symmetric reconvergence.",
        ],
        commands=[],
        expected_effect=(
            "The design owner adds a test-enable bypass, or the block is "
            "covered by a dedicated IP test instead of ATPG."
        ),
        effort="high",
        risk="high",
        requires_measurement=False,
        feasibility_rank=4,
    )),

    # -- EAB --------------------------------------------------------------
    _entry(FixAction(
        fix_id="eab_abort_analysis",
        title="Run the EDT abort analysis",
        applies_to=["UC.EAB", "UO.EAB"],
        rationale=(
            "EDT aborts stem from the compression encoding, so the tool's own "
            "abort analysis is the only reliable way to identify which "
            "limitation applies."
        ),
        preconditions=[
            "Pattern creation can be re-run; the analysis needs a full pass.",
        ],
        commands=[
            "report_environment",
            "set_edt_abort_analysis_options -max_cubes_to_analyze unlimited",
            "add_processor localhost:10",
            "create_patterns",
            "report_edt_abort_analysis",
        ],
        expected_effect=(
            "A report identifying over-specified cells, linear dependencies "
            "or a chain-to-channel imbalance as the limiting factor."
        ),
        effort="high",
        risk="low",
        requires_measurement=True,
        feasibility_rank=2,
        caveat=(
            "Interpret the report against tool documentation rather than "
            "guessing. If the output does not clearly support a conclusion, "
            "say so — reporting low confidence is a valid outcome."
        ),
    )),
    _entry(FixAction(
        fix_id="eab_abort_limit",
        title="Raise the abort limit before the EDT analysis run",
        applies_to=["UC.EAB", "UO.EAB"],
        rationale=(
            "A low abort limit inflates the EAB population and hides the real "
            "encoding limitation behind premature aborts."
        ),
        preconditions=[
            f"report_environment shows a limit below {MAX_ABORT_LIMIT}.",
        ],
        commands=[
            "report_environment",
            f"set_atpg_limits -abort_limit {MAX_ABORT_LIMIT}",
        ],
        expected_effect=(
            "The EAB population shrinks to the faults genuinely limited by "
            "the compression encoding."
        ),
        effort="low",
        risk="medium",
        requires_measurement=True,
        feasibility_rank=1,
        caveat=f"Settings above {MAX_ABORT_LIMIT} are not permitted.",
    )),

    # -- Fallback ---------------------------------------------------------
    _entry(FixAction(
        fix_id="generic_review",
        title="Investigate the dominant cluster manually",
        applies_to=[],
        rationale=(
            "No specific remedy is catalogued for this category, so the next "
            "step is to inspect where the faults concentrate and confirm the "
            "cause with the tool before acting."
        ),
        preconditions=[],
        commands=[
            "# Take fault paths verbatim from the dominant cluster, then:",
            "analyze_fault <fault_path>",
        ],
        expected_effect=(
            "Enough evidence to classify the loss as recoverable or as "
            "expected by design."
        ),
        effort="medium",
        risk="low",
        requires_measurement=False,
        feasibility_rank=2,
    )),
])


def fixes_for_subclass(dotted_class: str) -> List[FixAction]:
    """Return the catalogued fixes for *dotted_class*, best candidate first.

    Ordering comes from the subclass catalogue, which lists fix ids in the
    order an experienced analyst would try them — cheap confirmation steps
    before expensive structural changes.

    Args:
        dotted_class: A class id such as ``AU.TC`` or ``UO.AAB``.

    Returns:
        Matching :class:`FixAction` objects. Falls back to the generic review
        action when the subclass is unknown or has no catalogued fixes.
    """
    from .subclasses import describe_subclass  # local: avoids import cycle

    info = describe_subclass(dotted_class)
    ids = list(info.fix_ids) if info else []
    actions = [FIX_CATALOG[i] for i in ids if i in FIX_CATALOG]
    return actions or [FIX_CATALOG["generic_review"]]
