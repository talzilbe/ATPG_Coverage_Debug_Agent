"""Strict ATPG/DFT coverage debug agent — system prompt, payload, LLM client.

The :data:`SYSTEM_PROMPT` is the verbatim, conservative, evidence-driven
operating contract for the agent.  :func:`build_user_payload` serialises an
:class:`AnalysisReport` into a compact, structured text block the model can
reason over.  :class:`DebugAgent` performs the (optional) LLM call using only
the Python standard library so no extra third-party packages are required.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..analysis import investigate

logger = logging.getLogger(__name__)

#: Repository root (parent of the package dir) — used to set PYTHONPATH for the
#: MCP server subprocess the Copilot CLI launches.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# System prompt (verbatim operating contract)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
System Prompt — ATPG / DFT Coverage Debug Agent

You are a strict, evidence-driven ATPG/DFT coverage debug agent for hardware
engineers working with hierarchical gate-level Verilog netlists, Tessent ATPG
fault lists, and constraint files.

Your role is to determine exactly where and why structural test coverage is lost.

You must operate conservatively:
- Do not guess
- Do not invent connectivity
- Do not assume hierarchy mappings without stating them
- Do not hide uncertainty
- Do not provide vague conclusions without structural evidence
If evidence is incomplete or ambiguous, explicitly say so.

1. MISSION
Analyze the provided hierarchical Verilog netlist, Tessent ATPG fault list, and
constraint file. Identify coverage-loss root causes, especially for faults marked:
  AU = undetected, coverage loss
  UO = unobserved, coverage loss
  UC = uncontrolled, coverage loss
You may use DS, DI = detected and TI = tied by hardware for context/contrast,
but your primary focus is explaining coverage loss.

4. FAULT CODE INTERPRETATION (do not redefine)
  DS = detected ; DI = detected ; TI = tied by hardware
  AU = undetected (coverage loss) ; UO = unobserved (coverage loss) ; UC = uncontrolled (coverage loss)
Treat AU/UO/UC as coverage-loss faults; DS/DI as detected; TI as tied by hardware.

5. MANDATORY WORKFLOW
  Step 1 Parse the netlist (hierarchy, instances, cell types, nets, pins, driver/load, fan-in/out).
  Step 2 Parse the fault list (object/pin/site, class, normalize, coverage relevance).
  Step 3 Correlate fault objects to netlist objects (mark confidence high/medium/low; never fabricate).
  Step 4 Parse the constraint file (constrained nets/pins/ports/instances, forced values, blocked enables,
          restricted clocks/resets, observation limits, propagation barriers, broad fan-out impact).
  Step 5 Compute structural context (immediate fan-in/out, upstream drivers, downstream observe points,
          nearest scan/non-scan boundary, whether blocked in activation/propagation/observation).
  Step 5a Resolve real drivers before assigning any root cause.
          For every AU/UO/UC fault on a sequential or gate pin:
          (a) Locate the actual instantiation in the netlist. Leaf names repeat
              across replicated modules -- the same register name can occur
              hundreds of times in one design. Disambiguate by first resolving
              the PARENT instance name to its module type, then find that module
              definition, then extract the leaf instantiation from inside that
              module body only.
          (b) Print the complete instantiation including all continuation lines.
          (c) Classify pins: scan-data-in (si/sd/ti/scan_in), scan-out
              (so/to/scan_out), shift-enable (se/ssb/sen/scan_enable). A cell is
              SCAN if it has a dedicated scan-data input AND a shift-enable pin.
          (d) Corroborate all three, and report the corroboration even when (c)
              already looks conclusive:
              - trace shift-enable back to a global test_se/scan_en (through
                buffers/inverters);
              - confirm scan-out reaches a module output port;
              - confirm scan-in is driven by a real net, not a tie cell. If si
                is driven by a tie cell, the cell is scan-CAPABLE but NOT
                chain-connected - state that distinction explicitly.
          (e) Trace every data and enable pin to its ultimate driving gate across
              hierarchy. Ports are commonly feedthroughs across 3-5 levels. At each
              level: find the module definition declaring the port, find where that
              module is instantiated, read the net bound to the port, repeat until
              a gate WITH INPUT PINS is found. Verify the terminal net's fanout by
              counting all its occurrences in the netlist.
  Step 6 Determine root cause from evidence. Allowed categories:
          - Constraint-induced controllability loss
          - Constraint-induced observability loss
          - Scannable logic connected to non-scan logic
          - Non-scan logic blocking propagation
          - Tied / constant hardware condition
          - Clock/reset/test-enable blocking
          - Unresolved or black-box connectivity
          - Structural masking / reconvergence
          - Other structural cause explicitly supported by evidence
          Do not use a category unless you can support it.
  Step 7 Summarize and prioritize (recurring bad boundaries, modules with concentrated loss,
          constraints affecting many faults, highest-impact bottlenecks).

6. HARD RULES
  - No guessing. If not proven by inputs, mark as unresolved or hypothesis.
  - Separate Observed (in input) / Derived (from connectivity) / Hypothesis (likely).
  - Be explicit about uncertainty and naming mismatches.
  - No shallow explanations: name the signal/path, the boundary, what blocks
    activation/propagation/observation, and why that yields AU/UO/UC.
  - Prioritize structural proof (fan-in/out, driver/load chain, scan boundary,
    forced/constrained values, local logic cone).
  - Scan status (scan vs non-scan) may ONLY be asserted from a netlist
    instantiation whose pin list has been literally read. Naming basis is
    NEVER sufficient. Fault-table fields (fanin, fanout, mapped_instance,
    confidence, 'scan boundary involved') are NEVER sufficient. In
    particular, fanin/fanout == 0 together with confidence 'unresolved'
    means the extractor FAILED TO MAP the object and carries ZERO
    connectivity information; it must never be read as 'no scan
    connection'. A 'scan boundary involved = N' column means 'no evidence
    found', not 'confirmed non-scan'. Absent netlist pin evidence, the
    required answer is exactly:
    'Unresolved - scan status cannot be determined without netlist pin
    evidence.'
  - Conservative recommendations only, linked to specific bottlenecks.

7. OUTPUT FORMAT (always)
  The user is ALREADY looking at a deterministic report generated from the same
  inputs. It contains, computed exactly: the fault-class census and coverage
  metric (S2), the evidence basis -- mapped / unmapped / tied-to-constant /
  actionable -- with the scan-status breakdown and the constant-driver ranking
  (S3), the per-category triage with hierarchy clusters and blocking sources
  (S4), the ranked fix plan with commands (S5), module and instance hotspots
  (S6), per-root-cause boxes (S7), the full per-fault coverage-loss table (S8),
  and a conclusions/priority table (S9). The same per-fault table is also
  exported to CSV and shown in the GUI.
  DO NOT REPRODUCE ANY OF THAT. Restating a number the tool already computed
  adds no information, and retyping hierarchical paths risks corrupting them.
  Cite a section instead ("see S3") and spend your output only on what the
  deterministic pass cannot do: judgement, cross-cutting reasoning, and
  disagreement.

  A. Verdict — 3-6 sentences, no tables. Which mechanism dominates the
     ACTIONABLE coverage loss (the mapped, non-tied population in S3) and the
     specific evidence for that claim. If the actionable population is small
     relative to unmapped + tied faults, say that the headline loss figure is
     dominated by artefacts and that no mechanism can be ranked yet. Never
     compute the ranking from the raw loss total.
  B. Evidence Gaps That Change The Answer — only limits NOT already quantified
     in S3. For each: what is missing, which specific conclusion it blocks, and
     the file or command that would close it. If S3 already states it, skip it.
  C. Corrections To The Computed Analysis — the rows where your reading differs
     from the tool's. For each: the fault site (copied verbatim), the computed
     root cause, your root cause, and the evidence for the change. Say "No
     corrections" when the computed classification holds. Do NOT restate rows
     you agree with; the full table is S8.
  D. Cross-Cutting Patterns — only patterns that span categories or hierarchies
     and are therefore invisible to the per-category clustering in S4/S6: one
     structure blocking several unrelated blocks, one constraint reaching
     several categories, a systematic naming or wiring anomaly. Skip if none.
  E. Detailed Debug Notes — short narratives for the two or three most
     important findings, each tracing the mechanism end to end: what is
     established at the site, what blocks activation or propagation, where the
     effect dies, and why that yields AU/UO/UC. This is the section with the
     most value; spend the output budget here.
  F. Fix Plan Review — do not invent a parallel plan. Take the ranked plan in
     S5 and, per proposal, state agree / re-rank / reject with the reason.
     Add a proposal only for something the plan misses, and say why it is
     missing.

8. DECISION LOGIC
  PRECEDENCE: before applying any UC/UO/AU rule below, complete Step 5a. If
  the terminal driver of a data or enable pin is a tie cell (no input pins;
  output-only; or a cell type matching the library's tie-high / tie-low
  naming), the root cause is 'Tied / constant hardware condition'.
  Scan-boundary and observability categories MUST NOT be used in that case.
  A stuck-at fault on a pin held at a hard constant is undetectable because
  no differing value can be established, regardless of scan architecture.
  UC -> prefer control/activation/constrained-control/tied-upstream/missing-scan-reach/blocked-TE-clk-rst.
  UO -> prefer observe/blocked-propagation/observation-mask/non-scan-observe-boundary/constrained-outputs.
  AU -> undetected; decide whether dominant reason is controllability, observability, mixed, masking,
        constraints, or scan/non-scan boundary. Do not force AU into UC/UO without evidence.

9. EVIDENCE LANGUAGE (mandatory for ambiguous cases)
  Observed / Derived / Likely / Unresolved.
  SELF-CHECK before emitting any scan-status or root-cause claim:
  1. Did I read the actual instantiation line, or only a fault-table row?
  2. Did I confirm the RIGHT instance among duplicate leaf names by
     resolving the parent module type?
  3. Am I relying on fanin/fanout/confidence/scan-column values from an
     unresolved fault-table row?
  4. If I claim non-scan, can I quote an instantiation with no si/se pins?
  5. If I claim an observability or scan cause, have I ruled out a tied
     constant on the data and enable pins?
  If any check fails, answer 'Unresolved' and state exactly which file or
  command is required. 'Observed' may label ONLY text literally read from a
  file.

11. STYLE: technical, concise, explicit, audit-friendly. Prefer tables and bullets.
   Avoid motivational language, filler, unsupported speculation.

12. FINAL INSTRUCTION
   Answer with evidence: "Where is coverage lost, and is the loss caused by constraints,
   scan/non-scan interaction, controllability loss, observability loss, or another structurally
   proven reason?" If data is insufficient, say exactly what is missing.
"""


# ---------------------------------------------------------------------------
# Agentic system prompt (tool-using variant)
# ---------------------------------------------------------------------------
AGENTIC_SYSTEM_PROMPT = SYSTEM_PROMPT + """

--- AGENTIC TOOL USE ---
You are running in AGENTIC mode. In addition to the structural evidence
provided, you have a set of analysis SKILLS available as callable tools. Each
tool runs a deterministic structural analysis over the SAME parsed netlist,
fault list, and constraints and returns audit-ready findings.

Rules for tool use:
- Prefer calling relevant skills to gather concrete structural evidence before
  drawing conclusions. Do NOT fabricate evidence a skill could provide.
- You may call multiple skills, and may call the same skill again with
  different arguments if that sharpens the analysis.
- Tool findings are Observed/Derived structural facts — treat them as evidence,
  not as final conclusions; you still must reason over them.
- When you have enough evidence, STOP calling tools and return the full A-F
  report exactly as specified in the base system prompt.
- Never claim a skill returned something it did not. If a tool returns no
  findings, say so.
"""


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------
@dataclass
class AgentConfig:
    """Configuration for the LLM backend.

    Two backends are supported:

    * ``"http"`` — an OpenAI-compatible ``/chat/completions`` endpoint.
    * ``"cli"``  — the local GitHub Copilot CLI, invoked as a subprocess so no
      endpoint/URL configuration is required and requests go through the CLI's
      own authenticated channel.

    Attributes:
        backend:     ``"http"`` or ``"cli"``.
        base_url:    OpenAI-compatible base URL (e.g. ``https://host/v1``).
        model:       Model name to request (HTTP backend).
        api_key:     Bearer token (kept in-session only; never persisted to disk).
        temperature: Sampling temperature.
        max_tokens:  Maximum completion tokens.
        max_faults:  Cap on coverage-loss faults serialised into the payload.
        timeout:     HTTP / subprocess timeout in seconds.
        cli_path:    Path to the ``copilot`` executable (CLI backend).
        cli_home:    Value for ``COPILOT_HOME`` (config/state dir; CLI backend).
        cli_model:   Optional model id passed to the CLI via ``--model``.
        cli_token:   Optional GitHub token injected as ``COPILOT_GITHUB_TOKEN``
                     for the CLI subprocess (kept in memory only).
    """

    backend: str = "http"
    base_url: str = ""
    model: str = "gpt-4"
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 4000
    max_faults: int = 200
    timeout: int = 120
    cli_path: str = ""
    cli_home: str = ""
    cli_model: str = ""
    cli_token: str = ""
    #: When True, the CLI agentic run exposes the investigative tools to the
    #: Copilot CLI via a local MCP server so the model drives them itself.
    cli_use_mcp: bool = True

    @property
    def configured(self) -> bool:
        """True when enough is set to attempt a live LLM call."""
        if self.backend == "cli":
            return bool(self.cli_path.strip())
        return bool(self.base_url.strip() and self.model.strip())


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------
def _triage_payload(report: Any) -> List[str]:
    """Serialise the offline triage conclusions and the ranked fix plan.

    The deterministic pass does not stop at per-fault evidence: it aggregates
    the loss into categories, scores how actionable each one is, locates where
    it concentrates, traces what is blocking it and emits a ranked fix plan.
    Those conclusions are report sections S4 and S5, and the system prompt asks
    the model to *review* them -- section C for disagreements, section F for the
    plan. Without them in the payload the model would be critiquing something it
    was never shown, so they are included in both agentic and non-agentic runs.

    Returns an empty list for a report that predates the triage (an older
    session file), keeping the payload backwards compatible.
    """
    stats = getattr(report, "statistics", None)
    if stats is None:
        return []

    lines: List[str] = ["## Offline Triage Conclusions (report section S4)"]
    lines.append(
        "These are the deterministic pass's OWN conclusions, already shown to "
        "the user. They are INPUT: do not restate them. Disagree with them in "
        "section C, and review the plan below in section F."
    )
    lines.append(
        f"- Detected: {stats.detected_count} ({stats.detected_pct:.2f}%); "
        f"coverage loss: {stats.loss_count} ({stats.loss_pct:.2f}%). "
        f"Aggregated from the fault list -- NOT the tool's test-coverage figure."
    )

    loss_stats = getattr(stats, "loss_stats", None) or []
    if loss_stats:
        lines.append("- Loss categories (category | faults | % of all | sa0 | "
                     "sa1 | imbalance):")
        for st in loss_stats:
            lines.append(f"    {st.subclass_id} | {st.count} | {st.pct:.2f}% | "
                         f"{st.sa0} | {st.sa1} | {st.sa_asymmetry:.2f}")

    selected = getattr(report, "selected_categories", None) or []
    for cat in selected:
        lines.append(f"### {cat.rank}. {cat.subclass_id} -- {cat.reason}")

        verdict = getattr(cat, "verdict", None)
        if verdict is not None:
            patterns = ", ".join(verdict.patterns) if verdict.patterns else "-"
            lines.append(
                f"- Scored verdict: worth acting on = {verdict.actionable} "
                f"({verdict.confidence.value} confidence). {verdict.reason} "
                f"Patterns: {patterns}.")

        clusters = getattr(cat, "clusters", None)
        if clusters is not None and clusters.clusters:
            lines.append(
                f"- Concentration (WHERE to look, not a root cause): "
                f"{len(clusters.clusters)} cluster(s) at depth {clusters.depth}.")
            for cluster in clusters.clusters[:3]:
                lines.append(f"    {cluster.prefix} | {cluster.count} faults | "
                             f"{cluster.pct:.1f}% | sa0={cluster.sa0} "
                             f"sa1={cluster.sa1}")

        att = getattr(cat, "attribution", None)
        if att is not None and att.attributed:
            lines.append(
                f"- Blocking sources (STRUCTURAL ESTIMATE from fan-in cones, "
                f"not the ATPG tool's attribution): {att.attributed} of "
                f"{att.analysed} traced ({att.coverage:.0%}), verdict "
                f"{att.verdict}. {att.note}")
            for src in [s for s in att.tie_sources if s.kind != "tie_cell"][:3]:
                lines.append(
                    f"    driver {src.driver} ({src.cell_type or '-'}) holds "
                    f"{src.tie_value or '?'}, kind={src.kind}, "
                    f"reprogrammable={'yes' if src.is_configurable else 'no'}, "
                    f"{src.count} fault(s)")
            for src in att.constraint_sources[:3]:
                lines.append(f"    constraint {src.signal} "
                             f"({src.kind or '-'}) = {src.value or '?'}, "
                             f"{src.count} fault(s)")

        prof = getattr(cat, "reachability", None)
        if prof is not None and prof.profiled:
            lines.append(
                f"- Structural signature of the aborted sites: "
                f"{prof.profiled} of {prof.analysed} profiled, dominant "
                f"'{prof.dominant}' ({prof.dominant_share:.0%}). {prof.note} "
                f"A narrow bottleneck and a reconvergent cone need OPPOSITE "
                f"fixes, so check the signature before endorsing more abort "
                f"budget.")
    lines.append("")

    recommendations = getattr(report, "recommendations", None) or []
    if recommendations:
        lines.append("## Ranked Fix Plan (report section S5)")
        lines.append(
            "The plan you must review in section F. Take each proposal and say "
            "agree / re-rank / reject with the reason. Do not invent a parallel "
            "plan; add a proposal only for something this misses."
        )
        for rec in recommendations:
            gain = ("benefit must be MEASURED by a re-run; no gain is predicted"
                    if rec.requires_measurement else "benefit is estimated")
            lines.append(
                f"### {rec.rank}. {rec.title} [{rec.subclass_id}, "
                f"{rec.fault_count} faults, {rec.pct:.2f}%, "
                f"{rec.confidence.value} confidence, "
                f"actionable={rec.actionable}]")
            lines.append(f"- Rationale: {rec.fix.rationale}")
            lines.append(f"- {gain}")
            for caveat in rec.caveats[:3]:
                lines.append(f"- Caveat: {caveat}")
        lines.append("")

    return lines


def build_user_payload(report: Any, max_faults: int = 200,
                       agentic: bool = False) -> str:
    """Serialise an :class:`AnalysisReport` into a structured text payload.

    The structural analyser has already correlated faults to netlist objects;
    this function presents that evidence compactly so the LLM reasons over
    *observed structural facts* rather than re-deriving connectivity.

    Args:
        report:     A populated ``AnalysisReport``.
        max_faults: Maximum number of coverage-loss faults to include.
        agentic:    When ``True``, omit pre-computed skill findings and emit an
                    agentic task that instructs the model to call skills as
                    tools before concluding.

    Returns:
        A multi-section plain-text payload.
    """
    s = report.summary
    lines: List[str] = []

    lines.append("# ATPG STRUCTURAL ANALYSIS EVIDENCE (machine-extracted)")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Total faults analysed: {s.total_faults}")
    lines.append(f"- Coverage-loss faults (AU/UO/UC): {s.coverage_loss_count}")
    lines.append("- Fault class counts:")
    for cls in ("DS", "DI", "TI", "AU", "UO", "UC", "UNKNOWN"):
        if cls in s.class_counts:
            lines.append(f"    {cls}: {s.class_counts[cls]}")
    lines.append("- Top root-cause categories (structural heuristic):")
    for name, count in s.top_root_causes:
        lines.append(f"    {count:5d}  {name}")
    lines.append("- Top affected instances (ACTIONABLE loss only; tie-driven "
                 "and unmapped faults excluded):")
    for name, count in s.top_instances[:10]:
        lines.append(f"    {count:5d}  {name}")
    lines.append("- Top contributing constraints:")
    for name, count in s.top_constraints:
        lines.append(f"    {count:5d}  {name}")
    lines.append("")

    # Evidence basis. Stated up front so the model cannot rank causes from a
    # bucket that is mostly unmapped rows or tie-driven faults.
    if s.coverage_loss_count:
        lines.append("## Evidence Basis of the Coverage Loss")
        lines.append(f"- Mapped onto the netlist: {s.mapped_count}")
        lines.append(
            f"- NOT mapped: {s.unmapped_count} -- connectivity is UNKNOWN for "
            f"these, not zero. No root cause on them is provable and they "
            f"must not be counted towards an observability or scan-boundary "
            f"conclusion.")
        lines.append(
            f"- Held at a hard constant (tie cell resolved across hierarchy): "
            f"{s.tied_constant_count} -- expected and non-actionable.")
        lines.append(
            f"- Actionable coverage loss (mapped and not tied): "
            f"{s.actionable_loss_count}")
        scan = dict(s.scan_evidence_counts or {})
        if scan:
            lines.append("- Scan status of the fault sites, read from each "
                         "instantiation's pin list:")
            for key in ("scan", "non_scan", "unknown"):
                if key in scan:
                    lines.append(f"    {scan[key]:5d}  {key}")
            lines.append("    ('unknown' means no instantiation was read; it "
                         "is NOT evidence of non-scan logic.)")
        causes = dict(s.unresolved_causes or {})
        if causes:
            lines.append("- Why the unmapped faults did not map:")
            for cause, count in sorted(causes.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {count:5d}  {cause}")
        lines.append("")

    # The offline triage conclusions and the ranked fix plan (S4/S5). The
    # output contract asks the model to correct these and review the plan, so
    # they must be in the payload in BOTH modes -- in agentic mode the tools
    # are for drilling deeper, not for re-deriving what is already here.
    lines.extend(_triage_payload(report))

    # Repeated patterns
    if report.pattern_groups:
        lines.append("## Repeated Pattern Groups")
        for g in report.pattern_groups[:30]:
            samples = ", ".join(g.sample_faults[:3])
            lines.append(f"- [{g.kind}] {g.key} (count={g.count}) e.g. {samples}")
        lines.append("")

    # Coverage-loss faults
    lines.append("## Coverage-Loss Faults (AU/UO/UC)")
    lines.append(
        "This table is INPUT for your reasoning. It is already published to "
        "the user as report section S8, as a CSV export and as a GUI table, so "
        "do not reproduce it -- quote a row only when you are correcting it."
    )
    lines.append(
        "Columns: site | class | mapped_instance | confidence | cell_type | "
        "fanin | fanout | ctrl | obsv | constraint | scan | root_cause"
    )
    lines.append(
        "Column semantics: fanin/fanout = NULL and scan = unknown mean the "
        "object was NOT mapped onto the netlist, so NO connectivity was "
        "measured. NULL is not zero and unknown is not 'no' -- these rows "
        "carry no evidence about scan status, drivers or observability. "
        "scan = N means no boundary was found in the mapped neighbourhood, "
        "which is still not proof that the cell is non-scan."
    )
    shown = 0
    for r in report.fault_results:
        if shown >= max_faults:
            lines.append(f"... ({len(report.fault_results) - shown} more faults omitted)")
            break
        fan_in = r.fan_in_count
        fan_out = r.fan_out_count
        lines.append(
            " | ".join([
                r.fault.fault_object,
                r.fault.fault_class.value,
                r.mapping.instance_name or "-",
                r.mapping.confidence.value,
                r.cell_type or "-",
                "NULL" if fan_in is None else str(fan_in),
                "NULL" if fan_out is None else str(fan_out),
                "Y" if r.controllability_issue else "N",
                "Y" if r.observability_issue else "N",
                "Y" if r.constraint_related else "N",
                {"yes": "Y", "no": "N"}.get(r.scan_boundary_state, "unknown"),
                r.root_cause.value,
            ])
        )
        shown += 1
    lines.append("")

    # Parsing warnings / limits
    if report.warnings:
        lines.append("## Parsing Warnings / Limits (sample)")
        for w in report.warnings[:20]:
            lines.append(f"- {w}")
        if len(report.warnings) > 20:
            lines.append(f"- ... and {len(report.warnings) - 20} more warnings")
        lines.append("")

    # Skill findings (if any)
    skill_results = getattr(report, "skill_results", None)
    if skill_results and not agentic:
        lines.append("## Skill Findings (auxiliary structural skills)")
        for sr in skill_results:
            lines.append(f"### {sr.skill_id}: {sr.summary}")
            for f in getattr(sr, "findings", [])[:10]:
                lines.append(f"- {f.title} [{f.confidence}] — {f.description}")
        lines.append("")

    lines.append("## TASK")
    lines.append(
        "Everything above is the completed offline analysis: the evidence it "
        "measured, the conclusions it reached and the fix plan it ranked. Your "
        "job is to REVIEW it, not to redo it or restate it."
    )
    if agentic:
        lines.append(
            "You have access to analysis SKILLS exposed as callable tools. Use "
            "them to drill into specific faults, paths and categories, and to "
            "TEST the conclusions above -- not to re-derive figures already "
            "given. Decide which skills are relevant, CALL them (you may call "
            "several, in any order, and call one again with different "
            "arguments if useful), then use their structured findings as "
            "additional evidence. When you have gathered enough evidence, "
            "produce the full A-F output described in the system prompt. Mark "
            "every ambiguous statement as Observed / Derived / Likely / "
            "Unresolved. Do not invent connectivity that is not present in the "
            "evidence or returned by a skill."
        )
    else:
        lines.append(
            "Using ONLY the analysis above, produce the full A-F output "
            "described in the system prompt. Mark every ambiguous statement as "
            "Observed / Derived / Likely / Unresolved. Do not invent "
            "connectivity that is not present in this evidence."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Debug agent (LLM client)
# ---------------------------------------------------------------------------
class DebugAgent:
    """Runs the strict debug system prompt against an OpenAI-compatible LLM."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def build_prompt(self, report: Any) -> str:
        """Return the full user payload (without calling any LLM)."""
        return build_user_payload(report, max_faults=self.config.max_faults)

    def run(self, report: Any, session_id: Optional[str] = None,
            on_chunk=None) -> str:
        """Call the LLM and return its completion text.

        Args:
            report:     Populated ``AnalysisReport``.
            session_id: Optional CLI session UUID so the conversation can be
                        resumed later for follow-up chat (CLI backend only).
            on_chunk:   Optional ``callable(str)`` invoked with partial output
                        as it streams in.

        Raises:
            RuntimeError: if the endpoint is not configured or the call fails.
        """
        if not self.config.configured:
            raise RuntimeError(
                "No LLM backend configured. Set a base URL and model (HTTP) or a "
                "Copilot CLI path, or use 'Build Prompt Only' to copy the prompt "
                "into your own chat model."
            )
        user_payload = self.build_prompt(report)
        if self.config.backend == "cli":
            return self._call_cli(SYSTEM_PROMPT, user_payload,
                                  session_id=session_id, on_chunk=on_chunk)
        return self._call_chat_completions(SYSTEM_PROMPT, user_payload,
                                           on_chunk=on_chunk)

    def run_with_prompt(self, system_prompt: str, user_payload: str) -> str:
        """Call the LLM with an explicit system + user prompt pair."""
        if not self.config.configured:
            raise RuntimeError("No LLM backend configured.")
        if self.config.backend == "cli":
            return self._call_cli(system_prompt, user_payload)
        return self._call_chat_completions(system_prompt, user_payload)

    def run_agentic(self, report: Any, skill_manager: Any, ctx: Any,
                    on_event=None, max_iterations: int = 8,
                    session_id: Optional[str] = None, on_chunk=None) -> str:
        """Run a tool-using agent loop where skills are exposed as tools.

        The model is given the structural evidence plus a tool schema for every
        *enabled* skill. When the model requests a tool call, the corresponding
        skill is executed against *ctx* and its findings are fed back. The loop
        repeats until the model returns a final (tool-free) answer or
        ``max_iterations`` is reached.

        Args:
            report:        Populated ``AnalysisReport`` (structural evidence).
            skill_manager: SkillManager providing the callable skills/tools.
            ctx:           ``AnalysisContext`` skills execute against.
            on_event:      Optional ``callable(str)`` for streaming trace lines
                           to the UI (tool calls, results, iteration markers).
            max_iterations: Safety cap on tool-call rounds.

        Returns:
            The model's final natural-language A-F diagnosis.

        Raises:
            RuntimeError: if the endpoint is not configured or the call fails.
        """
        if not self.config.configured:
            raise RuntimeError(
                "No LLM backend configured. Set a base URL and model (HTTP) or a "
                "Copilot CLI path to run the agentic agent.")

        def emit(msg: str) -> None:
            if on_event:
                on_event(msg)

        # The GitHub Copilot CLI runs its own internal tool-using loop, so we
        # cannot hand it our OpenAI-style tool schema. Instead we run the
        # enabled skills locally, fold their structural findings into the
        # prompt, and let the CLI reason over that evidence in one shot.
        if self.config.backend == "cli":
            return self._run_agentic_cli(report, skill_manager, ctx, emit,
                                         session_id=session_id,
                                         on_chunk=on_chunk)

        enabled = skill_manager.enabled_skills()
        tools = [s.to_tool_schema() for s in enabled]
        skills_by_id = {s.skill_id: s for s in enabled}
        emit(f"Agentic run started with {len(tools)} skill tool(s): "
             + ", ".join(skills_by_id) if tools else
             "Agentic run started with NO enabled skills (enable some in the "
             "Skills tab for tool use).")

        messages: List[dict] = [
            {"role": "system", "content": AGENTIC_SYSTEM_PROMPT},
            {"role": "user",
             "content": build_user_payload(report, self.config.max_faults,
                                           agentic=True)
             + _regression_note(getattr(ctx, "compare", None))},
        ]

        # Loop budget: cap total tool calls and cache identical calls so the
        # model cannot burn the budget on repeated or runaway tool use.
        max_tool_calls = max(len(tools) * 3, 12)
        tool_calls_made = 0
        call_cache: dict = {}

        for iteration in range(1, max_iterations + 1):
            emit(f"— Iteration {iteration}/{max_iterations}: asking the model…")
            message = self._post_chat(messages, tools=tools)
            messages.append(message)
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                emit("Model returned a final answer (no tool calls).")
                return message.get("content") or ""

            budget_hit = False
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
                emit(f"→ Tool call: {name}({', '.join(f'{k}={v}' for k, v in args.items())})")

                cache_key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                if cache_key in call_cache:
                    content = ("(cached — identical call already made this run)\n"
                               + call_cache[cache_key])
                    emit("   ↺ duplicate call — returning cached result")
                elif tool_calls_made >= max_tool_calls:
                    content = (f"ERROR: tool-call budget ({max_tool_calls}) "
                               "exhausted. Stop calling tools and answer now.")
                    emit(f"   ⚠ {content}")
                    budget_hit = True
                else:
                    tool_calls_made += 1
                    skill = skills_by_id.get(name)
                    if skill is None:
                        content = f"ERROR: unknown or disabled skill '{name}'."
                        emit(f"   ⚠ {content}")
                    else:
                        for key, value in args.items():
                            try:
                                skill.set_param(key, value)
                            except KeyError:
                                emit(f"   (ignored unknown param '{key}')")
                        try:
                            result = skill.run(ctx)
                            content = _serialize_skill_result(result)
                            call_cache[cache_key] = content
                            emit(f"   ✓ {len(result.findings)} finding(s), "
                                 f"{len(result.warnings)} warning(s) "
                                 f"[{tool_calls_made}/{max_tool_calls}]")
                        except Exception as exc:  # noqa: BLE001
                            content = f"ERROR: skill '{name}' raised: {exc}"
                            emit(f"   ⚠ {content}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": content,
                })

            if budget_hit:
                break

        emit("Reached max iterations — asking the model for a final answer.")
        messages.append({
            "role": "user",
            "content": "Stop calling tools now and produce your final A-F "
                       "diagnosis using the evidence gathered so far.",
        })
        final = self._post_chat(messages, tools=None)
        return final.get("content") or "(no final answer produced)"

    def chat(self, message: str, session_id: Optional[str] = None,
             history: Optional[List[dict]] = None, on_chunk=None) -> str:
        """Send a follow-up message and return the reply.

        CLI backend: resumes the prior CLI session (``session_id``) so the model
        keeps the full analysis context. HTTP backend: replays ``history`` (a
        full OpenAI messages list already including the new user turn).
        """
        if not self.config.configured:
            raise RuntimeError("No LLM backend configured.")
        if self.config.backend == "cli":
            if not session_id:
                raise RuntimeError(
                    "No CLI session to resume — run the agent first.")
            return self._call_cli("", message, session_id=session_id,
                                  resume=True, on_chunk=on_chunk)
        if not history:
            raise RuntimeError("No conversation history for HTTP chat.")
        if on_chunk is not None:
            return self._post_stream(history, on_chunk)
        reply = self._post_chat(history, tools=None)
        return reply.get("content") or ""

    # -- internal ------------------------------------------------------------

    def _run_agentic_cli(self, report: Any, skill_manager: Any, ctx: Any,
                         emit, session_id: Optional[str] = None,
                         on_chunk=None) -> str:
        """Agentic run for the Copilot CLI backend.

        When ``cli_use_mcp`` is set, the investigative tools are exposed to the
        Copilot CLI via a local MCP server so the model drives them itself
        (true agentic orchestration). Otherwise it falls back to running the
        enabled bulk skills locally and folding their findings into the prompt.
        """
        if self.config.cli_use_mcp:
            try:
                return self._run_agentic_cli_mcp(report, ctx, emit, session_id,
                                                 on_chunk=on_chunk)
            except Exception as exc:  # noqa: BLE001
                emit(f"⚠ MCP path failed ({exc}); falling back to local skills.")

        enabled = skill_manager.enabled_skills()
        bulk = [s for s in enabled if not getattr(s, "on_demand", False)]
        emit(f"CLI agentic run: executing {len(bulk)} enabled skill(s) "
             "locally, then handing evidence to the Copilot CLI.")

        evidence_blocks: List[str] = []
        for skill in bulk:
            emit(f"→ Running skill: {skill.skill_id}")
            try:
                result = skill.run(ctx)
            except Exception as exc:  # noqa: BLE001
                emit(f"   ⚠ skill '{skill.skill_id}' raised: {exc}")
                continue
            emit(f"   ✓ {len(result.findings)} finding(s), "
                 f"{len(result.warnings)} warning(s)")
            evidence_blocks.append(_serialize_skill_result(result))

        payload = build_user_payload(report, self.config.max_faults,
                                     agentic=False)
        if evidence_blocks:
            payload += ("\n\n## Skill Tool Findings (executed locally)\n"
                        + "\n\n".join(evidence_blocks))
        emit("Calling GitHub Copilot CLI for the final diagnosis…")
        return self._call_cli(AGENTIC_SYSTEM_PROMPT, payload,
                              session_id=session_id, on_chunk=on_chunk)

    def _run_agentic_cli_mcp(self, report: Any, ctx: Any, emit,
                             session_id: Optional[str] = None,
                             on_chunk=None) -> str:
        """CLI agentic run where the model drives the investigative tools via a
        local MCP server.

        Serialises the analysis evidence to a temp file, writes an MCP server
        config pointing at :mod:`atpg_coverage_debug_agent.mcp_server`, and runs
        the Copilot CLI with that config so the model can call
        ``list_faults`` / ``get_fault_detail`` / ``why_blocked`` /
        ``list_constraints`` / ``trace_path`` itself.
        """
        evidence = investigate.export_evidence(
            ctx.fault_results, ctx.constraints, ctx.netlist,
            adjacency=getattr(ctx, "adjacency", None),
            compare=getattr(ctx, "compare", None),
            triage=getattr(ctx, "triage", None))
        ev_fd, ev_path = tempfile.mkstemp(prefix="atpg_evidence_", suffix=".json")
        with os.fdopen(ev_fd, "w", encoding="utf-8") as fh:
            json.dump(evidence, fh)

        server_env = {
            "PYTHONPATH": _REPO_ROOT,
            "ATPG_EVIDENCE_FILE": ev_path,
        }
        if self.config.cli_home.strip():
            server_env["COPILOT_HOME"] = self.config.cli_home.strip()
        mcp_cfg = {
            "mcpServers": {
                "atpg": {
                    "tools": ["*"],
                    "type": "local",
                    "command": sys.executable,
                    "args": ["-m", "atpg_coverage_debug_agent.mcp_server"],
                    "env": server_env,
                }
            }
        }
        cfg_fd, cfg_path = tempfile.mkstemp(prefix="atpg_mcp_", suffix=".json")
        with os.fdopen(cfg_fd, "w", encoding="utf-8") as fh:
            json.dump(mcp_cfg, fh)

        tool_names = ", ".join(investigate.TOOL_SPECS)
        payload = build_user_payload(report, self.config.max_faults,
                                     agentic=True)
        payload += (
            "\n\n## AVAILABLE MCP TOOLS (server 'atpg')\n"
            "You can call these deterministic investigation tools to gather "
            "exact structural evidence before concluding: " + tool_names + ".\n"
            "Use them to drill into specific faults, constraints, and paths "
            "(e.g. list_faults(fault_class='UO'), get_fault_detail(fault=...), "
            "why_blocked(fault=...), trace_path(from_instance=..., "
            "to_instance=...)). Every result is Observed/Derived structural "
            "fact. When you have enough evidence, produce the full A-F report.")
        payload += _regression_note(getattr(ctx, "compare", None))

        emit(f"Launching Copilot CLI with ATPG MCP tools: {tool_names}")
        try:
            return self._call_cli(
                AGENTIC_SYSTEM_PROMPT, payload, session_id=session_id,
                extra_args=["--additional-mcp-config", "@" + cfg_path],
                on_chunk=on_chunk)
        finally:
            for p in (ev_path, cfg_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _call_cli(self, system_prompt: str, user_payload: str,
                  session_id: Optional[str] = None,
                  resume: bool = False,
                  extra_args: Optional[List[str]] = None,
                  on_chunk=None) -> str:
        """Run the local GitHub Copilot CLI as a subprocess and return its text.

        The full system prompt and structural evidence are passed as a single
        non-interactive prompt (``-p``) in silent mode (``-s``) so only the
        model's answer is captured. The CLI runs in a throwaway scratch working
        directory and is told not to modify files, so it acts purely as a
        reasoning backend.

        Args:
            session_id: When set (and ``resume`` is False), starts a new session
                        with this UUID so it can be resumed for follow-up chat.
            resume:     When True, resumes ``session_id`` and sends only
                        ``user_payload`` (the prior context is already in the
                        session), enabling multi-turn conversation.
        """
        exe = self.config.cli_path.strip()
        if not exe:
            raise RuntimeError("No Copilot CLI path configured.")
        if not os.path.isfile(exe):
            raise RuntimeError(f"Copilot CLI not found at: {exe}")

        if resume:
            prompt = user_payload
        else:
            prompt = (
                system_prompt
                + "\n\n"
                + user_payload
                + "\n\nIMPORTANT: Do NOT create, modify, delete, or run anything "
                "on disk. Treat the evidence above as your only inputs and "
                "respond with the analysis text only."
            )

        env = dict(os.environ)
        if self.config.cli_home.strip():
            env["COPILOT_HOME"] = self.config.cli_home.strip()
        if self.config.cli_token.strip():
            env["COPILOT_GITHUB_TOKEN"] = self.config.cli_token.strip()

        scratch = tempfile.mkdtemp(prefix="atpg_cop_")
        cmd = [
            exe, "-p", prompt, "-s", "--no-color", "--allow-all-tools",
            "--no-remote", "--log-level", "error", "-C", scratch,
        ]
        if resume and session_id:
            cmd += ["--resume", session_id]
        elif session_id:
            cmd += ["--session-id", session_id]
        if self.config.cli_model.strip():
            cmd += ["--model", self.config.cli_model.strip()]
        if extra_args:
            cmd += list(extra_args)

        if on_chunk is not None:
            return self._call_cli_streaming(cmd, env, scratch, on_chunk)

        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True,
                timeout=self.config.timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Copilot CLI could not be executed: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Copilot CLI timed out after {self.config.timeout}s") from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"Copilot CLI exited {proc.returncode}: {err[:800]}")
        out = (proc.stdout or "").strip()
        if not out:
            err = (proc.stderr or "").strip()
            raise RuntimeError(
                "Copilot CLI returned no output."
                + (f" stderr: {err[:400]}" if err else ""))
        return out

    def _call_cli_streaming(self, cmd: List[str], env: dict, scratch: str,
                            on_chunk) -> str:
        """Run the CLI with :class:`subprocess.Popen`, emitting stdout as it
        arrives via *on_chunk*, and return the full accumulated text."""
        parts: List[str] = []
        try:
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise RuntimeError(f"Copilot CLI could not be executed: {exc}") from exc
        try:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(80), ""):
                if chunk:
                    parts.append(chunk)
                    on_chunk(chunk)
            try:
                proc.wait(timeout=self.config.timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError(
                    f"Copilot CLI timed out after {self.config.timeout}s") from exc
            err = (proc.stderr.read() if proc.stderr else "") or ""
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        if proc.returncode not in (0, None):
            detail = (err or "".join(parts)).strip()
            raise RuntimeError(
                f"Copilot CLI exited {proc.returncode}: {detail[:800]}")
        out = "".join(parts).strip()
        if not out:
            raise RuntimeError(
                "Copilot CLI returned no output."
                + (f" stderr: {err.strip()[:400]}" if err.strip() else ""))
        return out

    def _call_chat_completions(self, system_prompt: str, user_payload: str,
                               on_chunk=None) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ]
        if on_chunk is not None:
            return self._post_stream(messages, on_chunk)
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Unexpected LLM response format: {raw[:500]}"
            ) from exc

    def _post_stream(self, messages: List[dict], on_chunk) -> str:
        """Stream an OpenAI-compatible completion (SSE) and return full text."""
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        parts: List[str] = []
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                        delta = obj["choices"][0].get("delta", {})
                        chunk = delta.get("content") or ""
                    except (KeyError, IndexError, json.JSONDecodeError):
                        chunk = ""
                    if chunk:
                        parts.append(chunk)
                        on_chunk(chunk)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc.reason}") from exc
        return "".join(parts)

    def _post_chat(self, messages: List[dict],
                   tools: Optional[List[dict]] = None) -> dict:
        """POST a full messages list (optionally with tools) and return the
        assistant *message* object (which may contain ``tool_calls``)."""
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Unexpected LLM response format: {raw[:500]}"
            ) from exc


def is_cli_auth_error(message: str) -> bool:
    """True if *message* looks like a Copilot CLI authentication failure."""
    if not message:
        return False
    low = message.lower()
    signatures = (
        "no authentication information found",
        "authenticate with copilot",
        "not authenticated",
        "authentication failed",
        "gh auth login",
        "copilot_github_token",
    )
    return any(sig in low for sig in signatures)


def _regression_note(compare: Optional[dict]) -> str:
    """Prompt note telling the model a baseline is loaded (regression mode)."""
    if not compare:
        return ""
    label = compare.get("label") or "baseline"
    n = len(compare.get("faults", []) or [])
    return (
        "\n\n## REGRESSION MODE\n"
        f"A baseline report '{label}' ({n} coverage-loss faults) is loaded. "
        "Use the regression tools (regression_summary, list_regressed, "
        "list_fixed, list_changed) to determine what changed versus the "
        "baseline before concluding.")


def _serialize_skill_result(result: Any) -> str:
    """Render a :class:`SkillResult` into compact text for a tool response."""
    lines: List[str] = [f"skill: {result.skill_id}"]
    if getattr(result, "summary", ""):
        lines.append(f"summary: {result.summary}")
    lines.append(f"success: {getattr(result, 'success', True)}")
    findings = getattr(result, "findings", []) or []
    if not findings:
        lines.append("findings: none")
    else:
        lines.append(f"findings ({len(findings)}):")
        for i, f in enumerate(findings, 1):
            lines.append(f"  {i}. [{f.confidence}] {f.title} — {f.description}")
            if getattr(f, "evidence", None):
                for ev in f.evidence[:6]:
                    lines.append(f"       evidence: {ev}")
            if getattr(f, "affected_objects", None):
                objs = ", ".join(f.affected_objects[:10])
                lines.append(f"       affected: {objs}")
            if getattr(f, "recommendation", ""):
                lines.append(f"       recommendation: {f.recommendation}")
    warnings = getattr(result, "warnings", []) or []
    if warnings:
        lines.append(f"warnings ({len(warnings)}):")
        for w in warnings[:10]:
            lines.append(f"  - {w}")
    return "\n".join(lines)
