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
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .. import session
from ..analysis import investigate
from ..analysis.census import build_census
from ..analysis.findings import FINDINGS_FILE, read_findings
from ..analysis.fix_plan_edits import FIX_EDITS_FILE, read_edits
from ..parser import netlist_cache

logger = logging.getLogger(__name__)

#: File the MCP server appends one line per tool call to (see mcp_server).
TOOL_LOG_FILE = "tool_calls.jsonl"

#: Seconds a stopped CLI gets to exit on SIGTERM before it is killed.
CANCEL_GRACE_S = 3.0


class AgentCancelled(Exception):
    """The user stopped the turn. Distinct from a failure: whatever streamed
    before the stop is still valid partial output and the conversation
    survives."""


class CancelToken:
    """Cooperative stop signal shared between the GUI and a running turn.

    The turn checks :meth:`raise_if_cancelled` at every boundary it already
    has -- between model rounds, before each tool call, between streamed
    chunks -- and registers the live subprocess so :meth:`cancel` can
    terminate it instead of waiting for the read loop to notice.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def attach(self, proc: Optional[subprocess.Popen]) -> None:
        with self._lock:
            self._proc = proc
        if proc is not None and self.cancelled:
            self._terminate(proc)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            return

        def _kill_later() -> None:
            try:
                proc.wait(timeout=CANCEL_GRACE_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

        threading.Thread(target=_kill_later, daemon=True).start()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AgentCancelled("Stopped by user.")


@dataclass
class McpSession:
    """The hand-off artefacts of one agent conversation, kept on disk.

    The evidence, the MCP server config, the netlist pickle, the findings
    file and the tool-call log all live in ``work_dir``. They used to be
    deleted the moment the first answer came back, which is why a follow-up
    question could never call a tool: the server it would have needed was
    already gone. Now the session outlives the first turn and is closed by
    whoever owns the conversation (the GUI panel, on a new run or on exit).
    """

    work_dir: str
    config_path: str
    evidence_path: str
    netlist_path: Optional[str] = None

    @property
    def findings_path(self) -> str:
        return os.path.join(self.work_dir, FINDINGS_FILE)

    @property
    def fix_edits_path(self) -> str:
        return os.path.join(self.work_dir, FIX_EDITS_FILE)

    @property
    def tool_log_path(self) -> str:
        return os.path.join(self.work_dir, TOOL_LOG_FILE)

    @property
    def alive(self) -> bool:
        return bool(self.config_path) and os.path.isfile(self.config_path)

    def extra_args(self) -> List[str]:
        """CLI arguments that attach the tool server to a (resumed) turn."""
        if not self.alive:
            return []
        return ["--additional-mcp-config", "@" + self.config_path]

    def findings(self) -> List[Any]:
        return read_findings(self.findings_path)

    def fix_edits(self) -> List[Any]:
        return read_edits(self.fix_edits_path)

    def close(self) -> None:
        session.cleanup(self.work_dir)

#: Cap on the loss categories listed in the prompt. The per-fault table has a
#: user knob, but the triage census had none, so a design with many small
#: categories could quietly outgrow the context the knob was meant to protect.
#: The full census stays one `coverage_triage` call away.
MAX_TRIAGE_CATEGORIES = 25

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
  - RECONCILE BEFORE YOU QUOTE. Before quoting or deriving anything from a
    fault-class listing, add it up and check it equals the stated total. The
    payload opens with a HANDOFF_MANIFEST that states whether the census in
    it is complete and where the complete form lives; read it first. If a
    listing is marked a SUBSET, it is not the census -- retrieve the complete
    one instead of reasoning from the subset.
  - IF THE SUM CHECK FAILS, STOP. Report that the figures you were handed do
    not reconcile, name both numbers and the block they came from, and go no
    further with them. Do NOT name, label or bucket the difference. An
    invented category printed in a tool's own table format is indistinguish-
    able from a real one to the next reader, and is a worse outcome than the
    original omission. Where tools are available, call report_handoff_gap.
  - NEVER compute a coverage metric from a class list that has not passed the
    sum check. In particular the undetectable set UD in
    (DT + c*PD) / (FU - UD) is the WHOLE undetectable population as the class
    map defines it, not a single class. Prefer the metrics the deterministic
    pass already printed, each of which carries its own substitution.
  - SAY WHICH POPULATION A COVERAGE FIGURE DESCRIBES. A run that performs a
    fault disposition reports two columns: "total" over the full population
    and "total relevant" with the waived subclass excluded. They are both
    correct and they differ by several points. A percentage quoted without
    naming its column cannot be reconciled against the tool's own report.
  - CHECK THE SNAPSHOT BEFORE RANKING ROOT CAUSES. report_context's snapshot
    section says whether the fault list analysed is pre- or post-disposition.
    The disposition step REWRITES the ATPG-untestable subclass distribution,
    so a ranking built from a pre-disposition snapshot can put the wrong
    category first. If the snapshot is pre-disposition or undetermined, say
    so in the same paragraph as the ranking -- do not bury it in a caveat.
  - A TRUNCATED RESULT IS NOT A READ RESULT. If a tool response is marked
    truncated or names a spill file, retrieve the complete payload before
    concluding anything from it. Counts are never truncated; a shortened list
    of samples says so.
  - "Unresolved" is reserved for evidence that DOES NOT EXIST -- not for
    evidence you have not looked at yet. Before declaring anything
    unresolved, exhaust what is retrievable: uncalled tools, spilled
    payloads, other sections of report_context. Declaring a retrievable fact
    unresolved tells the engineer to stop asking, which is worse than the
    gap itself.
  - "The evidence does not settle this" is a COMPLETE and ACCEPTABLE answer.
    It is not a failure to answer, and it is strongly preferred over a
    confident wrong one: a plausible root cause that is wrong costs an
    engineer days of work on the wrong block. When you reach that point, say
    which question is unsettled, what the evidence does not show, and what
    would settle it. Never pad such an answer with a speculative cause.
  - Before trusting any count, check how much of it rests on faults that
    actually mapped onto the netlist, and whether an analyst waiver removed
    faults from the totals.
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
     missing. When tools are available, PUT the review INTO the plan with
     `propose_fix`: a practical note on an entry you agree with (amend), your
     own proposal for what the plan misses (add), or a better fix in place
     of an offline entry (replace, with the reason). The prose here then
     summarises what you changed; the engineer reads the plan itself.

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
- Call `report_context` EARLY. It returns the COMPLETE fault census -- every
  class, by coverage role, with the sum check already done -- plus how much of
  the coverage loss actually mapped onto the netlist, how much sits on hard
  constants, and whether an analyst has waived faults. The census is never
  abridged there, so once you have called it no residual can remain
  unexplained. A percentage computed over mostly unmapped faults does not mean
  what it appears to mean, and you cannot know that from the figure alone.
- If any class listing fails its sum check, call `report_handoff_gap`
  immediately, report the inconsistency, and stop working with those numbers.
  That tool exists so that "the figures I was handed contradict each other" is
  an available action. It is NOT the same as `report_insufficient_evidence`:
  use that one when evidence is missing, this one when evidence disagrees with
  itself. Never invent a category to absorb a difference.
- A tool result carrying `_truncation` is NOT a read result. It names the
  fields it dropped and, usually, a `spill_path` holding the complete payload.
  Read the spill, or re-query more narrowly, before concluding. Counts and the
  census are never truncated, so a missing count means the tool did not return
  one -- not that it was cut.
- When the evidence does not settle a question, call
  `report_insufficient_evidence` and report that as your answer. It exists so
  that "not determined" is a real, available action rather than something you
  have to argue your way into. Use it in preference to a hedged guess -- but
  only after exhausting what is retrievable. Evidence you have not fetched is
  not missing evidence.
- Scan status comes from `scan_status`. It answers from the live netlist when
  one is held and otherwise from the instantiation recorded for that site, and
  its `source` field says which. If it reports the netlist as unavailable
  while another tool is quoting instantiations, that is a hand-off
  inconsistency: report it with `report_handoff_gap`.
- Before quoting any hierarchy path in your answer, pass it through
  `verify_paths`. A shortened or reconstructed path will not resolve when the
  reader pastes it into a tool.
- Call `list_open_questions` BEFORE choosing what to investigate. It lists
  where the offline analysis itself recorded a weak spot -- reduced confidence,
  a blocker only partly traced, a structurally mixed category, a truncated
  cone, a contradiction between the ATPG tool's subclass and this tool's root
  cause -- each with the tool that would settle it. Spend your budget there,
  not on re-checking conclusions the analysis is already sure of.
- `classification_crosscheck` compares the ATPG tool's own subclass with the
  structural root cause on every mapped fault. A `disagree` pair means one
  side is wrong or the structure is not modelled; an `unconfirmed` pair means
  the tool named a mechanism this analysis could not find. Neither says which
  side is right -- check a sample before you decide.
- Every conclusion you reach that corrects, confirms or adds to the offline
  analysis, and every question you must leave open, goes through
  `record_finding` so it reaches the report and the saved session rather than
  only this transcript. A correction must state the corrected value and cite
  the tool result that supports it; the offline value is never overwritten,
  your finding is shown beside it under your name.
- Your fix-plan review goes INTO the plan through `propose_fix` (amend / add /
  replace). Commands you propose are text for the engineer to run; the tool
  runs nothing. A proposal is refused if it quotes a path not in the inputs,
  elides a path, or predicts a coverage gain -- the same rules the offline
  plan is held to. A replaced offline entry is kept, demoted and marked
  superseded; nothing you propose deletes anything.
"""


#: Used for the single corrective round-trip when an answer trips a guardrail.
#: Annotating a bad answer leaves the bad answer in front of the reader; this
#: asks for it to be fixed instead.
CORRECTION_SYSTEM_PROMPT = """You are correcting a hardware-debug analysis you
have just written. An automated check found statements that are not supported
by the source artefacts.

Two kinds of problem are reported:

* UNVERIFIABLE PATH - a hierarchy path that does not appear in the netlist,
  fault list or constraint file. Usually the path was shortened, elided with
  "...", or reconstructed from memory. A path that does not resolve when
  pasted into an ATPG tool is worse than no path at all. Either quote it
  exactly as it appears in the evidence you were given, or remove the claim
  and say the exact path was not available.

* UNMEASURED CLAIM - a predicted coverage gain. Nothing can establish a
  coverage number except re-running ATPG. Remove the prediction. You may still
  say an action is expected to help, without attaching a figure.

Rewrite the analysis so every flagged item is fixed. Preserve everything else
exactly: the structure, the section headings, the findings and the wording that
was not flagged. Do not add new claims, do not soften unrelated conclusions and
do not add a note about this correction.

Return ONLY the corrected analysis.
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
    #: When True, an answer that trips a guardrail gets ONE corrective
    #: round-trip before it reaches the user, rather than only a warning
    #: appended beneath the unsupported claim. Costs an extra call, and only
    #: when something was actually flagged.
    guardrail_retry: bool = True

    @property
    def configured(self) -> bool:
        """True when enough is set to attempt a live LLM call."""
        if self.backend == "cli":
            return bool(self.cli_path.strip())
        return bool(self.base_url.strip() and self.model.strip())


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------
#: Open questions printed in the digest; the rest are one tool call away.
MAX_DIGEST_OPEN_QUESTIONS = 12


def _open_questions_payload(report: Any, agentic: bool = False) -> List[str]:
    """Where the offline analysis is weakest, so the review starts there.

    Also prints the subclass/root-cause cross-check totals: a reviewer who
    sees 25 faults the ATPG tool calls tied that this tool could not resolve
    to a constant knows immediately where the structural model is thin.
    """
    lines: List[str] = []
    agreement = getattr(report, "agreement", None)
    questions = list(getattr(report, "open_questions", None) or [])
    if agreement is None and not questions:
        return lines

    lines.append("## Open Questions (where the OFFLINE analysis is weakest)")
    if agreement is not None:
        totals = dict(getattr(agreement, "totals", {}) or {})
        lines.append(
            "- Subclass vs structural root cause, per mapped fault: "
            + ", ".join(f"{k}={v}" for k, v in totals.items()))
        lines.append(f"    {getattr(agreement, 'note', '')}")
    if not questions:
        lines.append("- The offline pass recorded no weak spot. That is not "
                     "proof there is none.")
    else:
        lines.append(
            f"- {len(questions)} open question(s), highest priority first "
            "(1 = first). Spend investigation effort HERE before re-checking "
            "conclusions the analysis is already confident about:")
        for q in questions[:MAX_DIGEST_OPEN_QUESTIONS]:
            tools = ", ".join(q.suggested_tools) if agentic else ""
            lines.append(f"    [{q.priority}] {q.subject}: {q.question}")
            lines.append(f"        why: {q.why}")
            if tools:
                lines.append(f"        settle with: {tools}")
        if len(questions) > MAX_DIGEST_OPEN_QUESTIONS:
            lines.append(
                f"    ... {len(questions) - MAX_DIGEST_OPEN_QUESTIONS} more "
                "(complete list: list_open_questions tool / report section "
                "'Open Questions').")
    lines.append("")
    return lines


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
        shown = loss_stats[:MAX_TRIAGE_CATEGORIES]
        lines.append(
            f"- Loss categories — COVERAGE-LOSS SUBSET, {len(shown)} of "
            f"{len(loss_stats)} loss categorie(s) and NOT the census: "
            f"detected, possibly-detected and undetectable classes are "
            f"excluded by design. These counts must NOT be summed against "
            f"'Total faults analysed'. Complete census: {CENSUS_HOME}. "
            f"(category | faults | % of all | sa0 | sa1 | imbalance):")
        for st in shown:
            lines.append(f"    {st.subclass_id} | {st.count} | {st.pct:.2f}% | "
                         f"{st.sa0} | {st.sa1} | {st.sa_asymmetry:.2f}")
        if len(loss_stats) > len(shown):
            hidden = len(loss_stats) - len(shown)
            lines.append(
                f"    ... {hidden} smaller loss categorie(s) omitted from "
                f"this list. Call coverage_triage, or read {CENSUS_HOME}, for "
                f"the complete class breakdown.")

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


#: How many fault classes the digest prints inline. The census is normally far
#: smaller than this, so the digest normally carries it whole; a design with an
#: unusually fragmented class list gets a labelled subset instead of a silently
#: truncated one.
MAX_DIGEST_CENSUS_ROWS = 40

#: Where the complete census always lives, named in every subset marker.
CENSUS_HOME = "report_context.census (tool) / report section S2b"


def _census_block(report: Any) -> Tuple[List[str], bool]:
    """Render the fault-class census for the digest.

    Returns ``(lines, partial)``. When the census is too large to print in
    full, the block is explicitly labelled as a subset and names where the
    complete form lives -- because the defect this exists to prevent was not a
    wrong number but an unlabelled one: a six-class listing under a
    twenty-five-class total, which the reader closed by inventing a category
    to hold the difference.
    """
    census = build_census(report)
    lines: List[str] = []
    if not census.entries:
        return ["- Fault class census: no faults were parsed."], False

    shown = census.entries[:MAX_DIGEST_CENSUS_ROWS]
    partial = len(shown) < census.class_count
    marker = census.subset_note([e.subclass for e in shown], CENSUS_HOME)

    if partial:
        lines.append(f"- Fault class census [{marker}]:")
    else:
        lines.append(
            f"- Fault class census (COMPLETE — all {census.class_count} "
            f"class(es), grouped by coverage role):")
    for role in census.roles:
        printed = [e for e in role.entries if e in shown]
        if not printed:
            continue
        lines.append(f"    {role.role} ({role.label}): {role.count}")
        for entry in printed:
            lines.append(f"        {entry.subclass}: {entry.count} "
                         f"({entry.pct:.2f}%, sa0={entry.sa0} "
                         f"sa1={entry.sa1})")
    rec = census.reconciliation()
    if partial:
        lines.append(
            f"    NOT SHOWN: {census.class_count - len(shown)} smaller "
            f"class(es). The complete census is in {CENSUS_HOME}.")
    if rec["reconciles"]:
        lines.append(
            f"    Sum check: the {rec['classes']} class counts sum to "
            f"{rec['sum_of_class_counts']}, equal to the "
            f"{rec['total_faults']} fault(s) analysed. No residual exists.")
    else:
        lines.append(
            f"    SUM CHECK FAILED: {rec['classes']} class counts sum to "
            f"{rec['sum_of_class_counts']} against {rec['total_faults']} "
            f"fault(s) analysed, a delta of {rec['delta']}. Call "
            f"report_handoff_gap and stop; do not name the difference.")
    if rec["unclassified_tokens"]:
        lines.append(
            f"    UNCLASSIFIED class token(s): "
            f"{', '.join(rec['unclassified_tokens'])} — outside the "
            f"configured role map, so they feed no coverage metric.")
    return lines, partial


def build_handoff_manifest(report: Any, max_faults: int,
                           census_partial: bool,
                           agentic: bool = False) -> List[str]:
    """Declare, up front, exactly how complete this payload is.

    Every figure below is either whole or names where its whole form lives.
    The rule: **if a number in this digest is a subset, the digest says so.**
    A reader that has to detect an omission by finding a sum that does not
    close will eventually fail to detect one -- and the failure mode is not a
    missing answer, it is a confident fabricated one.
    """
    census = build_census(report)
    rec = census.reconciliation()
    summary = getattr(report, "summary", None)
    loss = len(getattr(report, "fault_results", None) or [])
    shown = min(loss, max_faults)

    if census_partial:
        census_line = (f"partial ({min(census.class_count, MAX_DIGEST_CENSUS_ROWS)}"
                       f" of {census.class_count} classes)")
    else:
        census_line = f"complete ({census.class_count} of {census.class_count} classes)"

    metrics_basis = "unavailable"
    stats = getattr(report, "statistics", None)
    if stats is not None and hasattr(stats, "metrics"):
        metrics_basis = ("full_census" if rec["reconciles"]
                         else "UNRECONCILED_CENSUS — do not quote")

    constraint_state = "not_supplied"
    diag = getattr(report, "constraint_diagnostics", None) or {}
    if diag:
        unresolved = int(diag.get("unresolved", 0) or 0)
        total = int(diag.get("directives", 0) or 0)
        constraint_state = (
            f"fully_parsed ({total} directives)" if not unresolved else
            f"partially_parsed ({unresolved} of {total} unevaluated)")

    netlist_parsed = bool(getattr(getattr(report, "netlist", None),
                                  "modules", None))
    sources = dict(getattr(report, "sources", None) or {})

    lines = [
        "HANDOFF_MANIFEST",
        "This block states how complete the rest of this payload is. Check it "
        "before deriving anything. Where a figure is a subset, the complete "
        "form is named; retrieve it rather than reconstructing it.",
        f"  design: {sources.get('design') or 'unnamed'}",
        f"  total_faults: {rec['total_faults']}",
        f"  census_in_digest: {census_line}",
        f"  census_sums_to_total: {rec['reconciles']} "
        f"(sum={rec['sum_of_class_counts']}, delta={rec['delta']})",
        f"  census_complete_via: {CENSUS_HOME}",
        f"  coverage_loss_faults: "
        f"{getattr(summary, 'coverage_loss_count', loss)}",
        f"  faults_in_fault_table: {shown} of {loss} "
        f"({'complete' if shown >= loss else 'sampled'})",
        f"  metrics_basis: {metrics_basis}",
        f"  unclassified_classes: "
        f"{', '.join(rec['unclassified_tokens']) or 'none'}",
        f"  constraint_file: {constraint_state}",
        f"  netlist_parsed: {'yes' if netlist_parsed else 'no'}",
        f"  tools_available: {'yes (MCP)' if agentic else 'no'}",
    ]
    if not rec["reconciles"]:
        lines.append(
            "  ACTION REQUIRED: the census does not reconcile. Do not compute "
            "any coverage metric from it, do not name the residual, and "
            "report the inconsistency (report_handoff_gap when tools are "
            "available).")
    lines.append("")
    return lines


def _metrics_lines(report: Any) -> List[str]:
    """The coverage metrics, each with the arithmetic that produced it.

    Printed rather than left for the model to derive, because deriving them
    means picking a denominator, and picking a denominator from an incomplete
    class list is how a headline figure ended up two points out.
    """
    stats = getattr(report, "statistics", None)
    if stats is None or not hasattr(stats, "metrics"):
        return []
    relevant = getattr(report, "relevant_statistics", None)
    columns = [("total", stats)]
    if relevant is not None:
        columns.append(("total relevant", relevant))

    state = getattr(report, "disposition", None)
    lines = []
    if state is not None:
        lines.append(
            f"- Snapshot analysed: {state.resolved_path or 'n/a'} "
            f"[{state.label}]"
            + (f", waiver subclass {state.waiver_subclass} "
               f"({state.waiver_count} fault(s))"
               if state.waiver_subclass else ""))
        if state.is_pre:
            lines.append(
                "    WARNING: pre-disposition snapshot. The disposition step "
                "rewrites the AU subclass distribution, so the category "
                "ranking below may not reflect the final design state. Say "
                "so in any answer that ranks root causes.")

    m = stats.metrics(stats)
    credited = ", ".join(m["credited_posdet_families"]) or "none"
    lines.append(
        f"- Coverage metrics (computed from the COMPLETE census above; "
        f"posdet_credit={m['posdet_credit']} for test/fault coverage; "
        f"effectiveness credits {credited} at full weight):")
    for label, pop in columns:
        payload = pop.metrics(stats)
        lines.append(f"    [{label}] FU={payload['total_faults']}")
        for key, name in (("test_coverage", "test coverage"),
                          ("fault_coverage", "fault coverage"),
                          ("atpg_effectiveness", "atpg effectiveness")):
            spec = payload.get("formulas", {}).get(key, {})
            value = payload[key]
            shown = "n/a" if value is None else f"{value:.4f}%"
            lines.append(f"      {name}: {shown}  "
                         f"[{spec.get('formula', '')}]  "
                         f"{spec.get('substitution', '')}")
        lines.append("      roles: " + ", ".join(
            f"{r}={payload['roles'].get(r, 0)}"
            for r in ("DT", "PD", "UD", "AU", "ND")))
    lines.append(f"    {m.get('ud_definition', '')}")
    lines.append(f"    {m.get('basis', '')}")
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

    census_lines, census_partial = _census_block(report)

    lines.append("# ATPG STRUCTURAL ANALYSIS EVIDENCE (machine-extracted)")
    lines.append("")
    lines.extend(build_handoff_manifest(report, max_faults, census_partial,
                                        agentic=agentic))
    lines.append("## Summary")
    lines.append(f"- Total faults analysed: {s.total_faults}")
    lines.append(f"- Coverage-loss faults (AU/UO/UC): {s.coverage_loss_count}")
    lines.extend(census_lines)
    lines.extend(_metrics_lines(report))
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
    lines.extend(_open_questions_payload(report, agentic=agentic))

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

    def __init__(self, config: AgentConfig,
                 mcp_session: Optional[McpSession] = None,
                 cancel: Optional[CancelToken] = None) -> None:
        self.config = config
        #: The on-disk hand-off of the current conversation. Set by an
        #: agentic CLI run; passed back in by the owner for follow-up turns.
        self.mcp_session: Optional[McpSession] = mcp_session
        #: Stop signal for the turn in flight. Always present so call sites
        #: never have to test for None.
        self.cancel = cancel or CancelToken()

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
            answer = self._call_cli(SYSTEM_PROMPT, user_payload,
                                    session_id=session_id, on_chunk=on_chunk)
        else:
            answer = self._call_chat_completions(SYSTEM_PROMPT, user_payload,
                                                 on_chunk=on_chunk)
        return self.correct_guardrail_issues(answer, report)

    def run_with_prompt(self, system_prompt: str, user_payload: str) -> str:
        """Call the LLM with an explicit system + user prompt pair."""
        if not self.config.configured:
            raise RuntimeError("No LLM backend configured.")
        if self.config.backend == "cli":
            return self._call_cli(system_prompt, user_payload)
        return self._call_chat_completions(system_prompt, user_payload)

    def correct_guardrail_issues(self, answer: str, report: Any,
                                 emit=None) -> str:
        """Ask the model to fix any unsupported claims in *answer*.

        Flagging a fabricated path underneath an answer still leaves the
        fabricated path in front of the reader, who may well act on it. One
        corrective round-trip fixes the answer instead. Only runs when the
        check actually finds something, so it costs nothing on a clean answer.

        Args:
            answer: The model's answer.
            report: The report the answer is about, for the path registry.
            emit: Optional ``callable(str)`` for trace output.

        Returns:
            The corrected answer, or the original when there was nothing to
            correct or the correction could not be made. Never raises: a
            failed correction must not lose the user their analysis.
        """
        if self.cancel.cancelled:
            return answer
        if not answer or report is None or not self.config.guardrail_retry:
            return answer
        try:
            from ..analysis.guardrails import (
                PathRegistry,
                check_text,
                issues_as_warnings,
            )
            registry = PathRegistry.from_report(report)
            issues = check_text(answer, registry, "agent answer")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Guardrail check skipped: %s", exc)
            return answer
        if not issues:
            return answer

        listed = "\n".join(f"- {line}" for line in issues_as_warnings(issues))
        if emit:
            emit(f"⚠ Guardrail found {len(issues)} unsupported statement(s); "
                 "asking for a correction.")
        payload = (f"## Problems found\n{listed}\n\n"
                   f"## Analysis to correct\n{answer}\n")
        try:
            corrected = self.run_with_prompt(CORRECTION_SYSTEM_PROMPT, payload)
        except Exception as exc:  # noqa: BLE001
            if emit:
                emit(f"   ⚠ correction call failed ({exc}); keeping the "
                     "original answer with its warnings.")
            return answer
        corrected = (corrected or "").strip()
        if not corrected:
            return answer

        try:
            remaining = check_text(corrected, registry, "agent answer")
        except Exception:  # noqa: BLE001
            remaining = []
        if emit:
            if remaining:
                emit(f"   {len(remaining)} issue(s) still present after the "
                     "correction; they are flagged on the answer.")
            else:
                emit("   ✓ corrected answer passes the guardrail check.")
        return corrected

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
            answer = self._run_agentic_cli(report, skill_manager, ctx, emit,
                                           session_id=session_id,
                                           on_chunk=on_chunk)
            return self.correct_guardrail_issues(answer, report, emit)

        messages: List[dict] = [
            {"role": "system", "content": AGENTIC_SYSTEM_PROMPT},
            {"role": "user",
             "content": build_user_payload(report, self.config.max_faults,
                                           agentic=True)
             + _regression_note(getattr(ctx, "compare", None))},
        ]
        answer = self._tool_loop(messages, skill_manager, ctx, emit,
                                 max_iterations=max_iterations)
        return self.correct_guardrail_issues(answer, report, emit)

    def _tool_loop(self, messages: List[dict], skill_manager: Any, ctx: Any,
                   emit, max_iterations: int = 8, report: Any = None) -> str:
        """Run the OpenAI-style tool-calling loop over *messages*.

        *messages* is extended IN PLACE with every intermediate assistant
        tool-call message and every tool result, so a caller that keeps a
        conversation history (the follow-up chat) sees the evidence the model
        gathered. The final answer is returned and NOT appended -- the caller
        owns that message. Shared by the first answer and every follow-up so
        the two cannot drift apart in budget, caching or error handling.
        """
        enabled = skill_manager.enabled_skills()
        tools = [s.to_tool_schema() for s in enabled]
        skills_by_id = {s.skill_id: s for s in enabled}
        emit(f"Agentic run started with {len(tools)} skill tool(s): "
             + ", ".join(skills_by_id) if tools else
             "Agentic run started with NO enabled skills (enable some in the "
             "Skills tab for tool use).")

        # Loop budget: cap total tool calls and cache identical calls so the
        # model cannot burn the budget on repeated or runaway tool use.
        max_tool_calls = max(len(tools) * 3, 12)
        tool_calls_made = 0
        call_cache: dict = {}

        for iteration in range(1, max_iterations + 1):
            self.cancel.raise_if_cancelled()
            emit(f"— Iteration {iteration}/{max_iterations}: asking the model…")
            message = self._post_chat(messages, tools=tools)
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                emit("Model returned a final answer (no tool calls).")
                return message.get("content") or ""
            messages.append(message)

            budget_hit = False
            for call in tool_calls:
                self.cancel.raise_if_cancelled()
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
        self.cancel.raise_if_cancelled()
        messages.append({
            "role": "user",
            "content": (
                f"You have used all {max_iterations} investigation rounds "
                "available, so this is your last turn and no further tools "
                "can be called. Produce your final A-F diagnosis from the "
                "evidence gathered so far. Where the investigation was cut "
                "short before settling something, say so and say what you "
                "would have checked next -- do not present a partial "
                "investigation as a complete one."),
        })
        final = self._post_chat(messages, tools=None)
        return final.get("content") or "(no final answer produced)"

    def chat(self, message: str, session_id: Optional[str] = None,
             history: Optional[List[dict]] = None, on_chunk=None,
             report: Any = None, skill_manager: Any = None, ctx: Any = None,
             on_event=None, max_iterations: int = 6) -> str:
        """Send a follow-up message and return the reply.

        CLI backend: resumes the prior CLI session (``session_id``) so the model
        keeps the full analysis context, and re-attaches the MCP tool server
        when this agent holds a live :class:`McpSession` -- so a follow-up can
        investigate, not just recall. HTTP backend: replays ``history`` (a
        full OpenAI messages list already including the new user turn); when
        *skill_manager* and *ctx* are given the turn runs through the same
        tool loop as the first answer.

        When *report* is given the reply gets the same guardrail correction
        as the first answer -- follow-ups are where "how much would that
        recover?" lands.
        """
        if not self.config.configured:
            raise RuntimeError("No LLM backend configured.")

        def emit(msg: str) -> None:
            if on_event:
                on_event(msg)

        if self.config.backend == "cli":
            if not session_id:
                raise RuntimeError(
                    "No CLI session to resume — run the agent first.")
            extra = self.mcp_session.extra_args() if self.mcp_session else []
            if extra:
                emit("Follow-up turn with the ATPG MCP tools attached.")
            else:
                emit("Follow-up turn WITHOUT tools: the model can only recall "
                     "the first answer's evidence.")
            answer = self._call_cli("", message, session_id=session_id,
                                    resume=True, on_chunk=on_chunk,
                                    extra_args=extra or None)
        else:
            if not history:
                raise RuntimeError("No conversation history for HTTP chat.")
            if skill_manager is not None and ctx is not None:
                # history is extended in place with the tool exchange.
                answer = self._tool_loop(history, skill_manager, ctx, emit,
                                         max_iterations=max_iterations)
            elif on_chunk is not None:
                answer = self._post_stream(history, on_chunk)
            else:
                reply = self._post_chat(history, tools=None)
                answer = reply.get("content") or ""
        if report is not None:
            answer = self.correct_guardrail_issues(answer, report, emit)
        return answer

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
        # Be explicit that this is NOT the agentic loop. The model gets one
        # pass over evidence chosen in advance and cannot ask for anything
        # else, so a reader must not credit its answer as an investigation.
        emit("⚠ NOT agentic: the investigative tools need the local MCP "
             "server. Enable 'Agentic tools' (MCP), or expect a single pass.")
        emit(f"Single-pass run: executing {len(bulk)} enabled skill(s) "
             "locally, then handing the findings to the Copilot CLI. The "
             "model cannot request further evidence.")

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
        payload += (
            "\n\n## Evidence is fixed for this run\n"
            "You have no tools available and cannot request more evidence. "
            "Everything you will get is above. Where it does not settle a "
            "question, say so plainly and name what would settle it -- do "
            "not fill the gap with a plausible answer.\n")
        emit("Calling GitHub Copilot CLI for the final diagnosis…")
        return self._call_cli(AGENTIC_SYSTEM_PROMPT, payload,
                              session_id=session_id, on_chunk=on_chunk)

    def _run_agentic_cli_mcp(self, report: Any, ctx: Any, emit,
                             session_id: Optional[str] = None,
                             on_chunk=None) -> str:
        """CLI agentic run where the model drives the investigative tools via a
        local MCP server.

        Serialises the analysis evidence to a file, writes an MCP server
        config pointing at :mod:`atpg_coverage_debug_agent.mcp_server`, and
        runs the Copilot CLI with that config so the model can call
        ``list_faults`` / ``get_fault_detail`` / ``why_blocked`` /
        ``list_constraints`` / ``trace_path`` itself.

        Every artefact goes into one directory named after the design and
        this run. The directory is kept in :attr:`mcp_session` -- NOT deleted
        when the answer returns -- so follow-up turns can re-attach the same
        server, and so the parsed netlist handed over here (a pickle, or the
        cache entry) lets those tools run the real structural machinery.
        The owner of the conversation closes it.
        """
        if self.mcp_session is not None:
            self.mcp_session.close()
        sources = dict(getattr(report, "sources", None) or {})
        stamp = session.stamp(sources.get("design"), sources)
        work_dir = session.session_dir(stamp["design"], stamp["run_id"],
                                       reuse_env=False)
        evidence = investigate.export_evidence(
            ctx.fault_results, ctx.constraints, ctx.netlist,
            adjacency=getattr(ctx, "adjacency", None),
            compare=getattr(ctx, "compare", None),
            triage=getattr(ctx, "triage", None),
            context=getattr(ctx, "context", None),
            design=investigate.serialize_design(
                getattr(ctx, "netlist", None), report),
            stamp=stamp)
        ev_path = os.path.join(work_dir, "evidence.json")
        with open(ev_path, "w", encoding="utf-8") as fh:
            json.dump(evidence, fh)

        netlist_pkl = netlist_cache.handoff_path(
            getattr(ctx, "netlist", None), sources.get("netlist"), work_dir)

        server_env = {
            "PYTHONPATH": _REPO_ROOT,
            "ATPG_EVIDENCE_FILE": ev_path,
            session.SESSION_DIR_ENV: work_dir,
        }
        if netlist_pkl:
            server_env[netlist_cache.NETLIST_FILE_ENV] = netlist_pkl
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
        cfg_path = os.path.join(work_dir, "mcp-config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(mcp_cfg, fh)
        self.mcp_session = McpSession(work_dir=work_dir, config_path=cfg_path,
                                      evidence_path=ev_path,
                                      netlist_path=netlist_pkl)

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
            "fact. "
            + ("The parsed netlist IS available to these tools in this "
               "session, so scan_status / trace_path / verify_paths answer "
               "from the design itself. " if netlist_pkl else
               "The parsed netlist could not be handed to the tools in this "
               "session; they answer from recorded evidence. ")
            + "Start with list_open_questions, record what you establish with "
            "record_finding, and when you have enough evidence produce the "
            "full A-F report.")
        payload += _regression_note(getattr(ctx, "compare", None))

        emit(f"Launching Copilot CLI with ATPG MCP tools: {tool_names}")
        emit("Netlist handed to the tools: "
             + (netlist_pkl or "NO (tools answer from recorded evidence)"))
        return self._call_cli(
            AGENTIC_SYSTEM_PROMPT, payload, session_id=session_id,
            extra_args=["--additional-mcp-config", "@" + cfg_path],
            on_chunk=on_chunk)

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

        # Every CLI call goes through Popen so a Stop can terminate it; the
        # non-streaming path simply collects instead of forwarding chunks.
        self.cancel.raise_if_cancelled()
        return self._call_cli_streaming(cmd, env, scratch, on_chunk)

    def _call_cli_streaming(self, cmd: List[str], env: dict, scratch: str,
                            on_chunk=None) -> str:
        """Run the CLI with :class:`subprocess.Popen`, emitting stdout as it
        arrives via *on_chunk* (when given), and return the full text.

        The process is registered with the cancel token, so a Stop
        terminates it; the partial text is then raised inside
        :class:`AgentCancelled` so the caller can keep what arrived.
        """
        parts: List[str] = []
        try:
            proc = subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise RuntimeError(f"Copilot CLI could not be executed: {exc}") from exc
        self.cancel.attach(proc)
        try:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(80), ""):
                if chunk:
                    parts.append(chunk)
                    if on_chunk is not None:
                        on_chunk(chunk)
            try:
                proc.wait(timeout=self.config.timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError(
                    f"Copilot CLI timed out after {self.config.timeout}s") from exc
            err = (proc.stderr.read() if proc.stderr else "") or ""
        finally:
            self.cancel.attach(None)
            shutil.rmtree(scratch, ignore_errors=True)

        if self.cancel.cancelled:
            raise AgentCancelled("".join(parts).strip())
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
                    if self.cancel.cancelled:
                        raise AgentCancelled("".join(parts))
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
