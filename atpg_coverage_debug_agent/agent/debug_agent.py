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
  - Do not overclaim scan info unless explicit structures or strong naming/library basis (state the basis).
  - Conservative recommendations only, linked to specific bottlenecks.

7. OUTPUT FORMAT (always)
  A. Executive Summary — total faults; counts by DS/DI/TI/AU/UO/UC; total coverage loss (AU+UO+UC);
     top root-cause categories; top affected modules/instances; top contributing constraints;
     and whether the dominant issue is constraint-driven / scan-boundary-driven / controllability /
     observability / mixed.
  B. Assumptions and Parsing Limits — file quality issues, hierarchy/mapping assumptions, unresolved
     naming mismatches, unknown cell directions, black boxes/missing modules, confidence limits.
  C. Coverage-Loss Table — for every AU/UO/UC: fault site, class, mapped object, mapping confidence,
     instance, cell type, immediate fan-in, immediate fan-out, controllability issue (Y/N/Possible),
     observability issue (Y/N/Possible), constraint-related (Y/N/Possible), scan boundary involved
     (Y/N/Possible), root-cause category, structural evidence, recommended next debug step.
  D. Repeated Pattern Analysis — common constraints/boundaries/observation blockages/uncontrollable conditions.
  E. Detailed Debug Notes — short narratives for the most important/repeated failures.
  F. Final Diagnosis — exact places coverage is lost; highest-confidence root causes; top 3 next actions.

8. DECISION LOGIC
  UC -> prefer control/activation/constrained-control/tied-upstream/missing-scan-reach/blocked-TE-clk-rst.
  UO -> prefer observe/blocked-propagation/observation-mask/non-scan-observe-boundary/constrained-outputs.
  AU -> undetected; decide whether dominant reason is controllability, observability, mixed, masking,
        constraints, or scan/non-scan boundary. Do not force AU into UC/UO without evidence.

9. EVIDENCE LANGUAGE (mandatory for ambiguous cases)
  Observed / Derived / Likely / Unresolved.

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
    lines.append("- Top affected instances:")
    for name, count in s.top_instances[:10]:
        lines.append(f"    {count:5d}  {name}")
    lines.append("- Top contributing constraints:")
    for name, count in s.top_constraints:
        lines.append(f"    {count:5d}  {name}")
    lines.append("")

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
        "Columns: site | class | mapped_instance | confidence | cell_type | "
        "fanin | fanout | ctrl | obsv | constraint | scan | root_cause"
    )
    shown = 0
    for r in report.fault_results:
        if shown >= max_faults:
            lines.append(f"... ({len(report.fault_results) - shown} more faults omitted)")
            break
        lines.append(
            " | ".join([
                r.fault.fault_object,
                r.fault.fault_class.value,
                r.mapping.instance_name or "-",
                r.mapping.confidence.value,
                r.cell_type or "-",
                str(len(r.fan_in)),
                str(len(r.fan_out)),
                "Y" if r.controllability_issue else "N",
                "Y" if r.observability_issue else "N",
                "Y" if r.constraint_related else "N",
                "Y" if r.scan_boundary_involved else "N",
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
    if agentic:
        lines.append(
            "You have access to analysis SKILLS exposed as callable tools. "
            "Decide which skills are relevant to the coverage loss above, CALL "
            "them (you may call several, in any order, and call one again with "
            "different arguments if useful), then use their structured findings "
            "as additional evidence. When you have gathered enough evidence, "
            "produce the full A-F output described in the system prompt. Mark "
            "every ambiguous statement as Observed / Derived / Likely / "
            "Unresolved. Do not invent connectivity that is not present in the "
            "evidence or returned by a skill."
        )
    else:
        lines.append(
            "Using ONLY the structural evidence above, produce the full A-F output "
            "described in the system prompt. Mark every ambiguous statement as "
            "Observed / Derived / Likely / Unresolved. Do not invent connectivity "
            "that is not present in this evidence."
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

    def run(self, report: Any, session_id: Optional[str] = None) -> str:
        """Call the LLM and return its completion text.

        Args:
            report:     Populated ``AnalysisReport``.
            session_id: Optional CLI session UUID so the conversation can be
                        resumed later for follow-up chat (CLI backend only).

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
                                  session_id=session_id)
        return self._call_chat_completions(SYSTEM_PROMPT, user_payload)

    def run_with_prompt(self, system_prompt: str, user_payload: str) -> str:
        """Call the LLM with an explicit system + user prompt pair."""
        if not self.config.configured:
            raise RuntimeError("No LLM backend configured.")
        if self.config.backend == "cli":
            return self._call_cli(system_prompt, user_payload)
        return self._call_chat_completions(system_prompt, user_payload)

    def run_agentic(self, report: Any, skill_manager: Any, ctx: Any,
                    on_event=None, max_iterations: int = 8,
                    session_id: Optional[str] = None) -> str:
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
                                         session_id=session_id)

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
                                           agentic=True)},
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
             history: Optional[List[dict]] = None) -> str:
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
                                  resume=True)
        if not history:
            raise RuntimeError("No conversation history for HTTP chat.")
        reply = self._post_chat(history, tools=None)
        return reply.get("content") or ""

    # -- internal ------------------------------------------------------------

    def _run_agentic_cli(self, report: Any, skill_manager: Any, ctx: Any,
                         emit, session_id: Optional[str] = None) -> str:
        """Agentic run for the Copilot CLI backend.

        When ``cli_use_mcp`` is set, the investigative tools are exposed to the
        Copilot CLI via a local MCP server so the model drives them itself
        (true agentic orchestration). Otherwise it falls back to running the
        enabled bulk skills locally and folding their findings into the prompt.
        """
        if self.config.cli_use_mcp:
            try:
                return self._run_agentic_cli_mcp(report, ctx, emit, session_id)
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
                              session_id=session_id)

    def _run_agentic_cli_mcp(self, report: Any, ctx: Any, emit,
                             session_id: Optional[str] = None) -> str:
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
            adjacency=getattr(ctx, "adjacency", None))
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

        emit(f"Launching Copilot CLI with ATPG MCP tools: {tool_names}")
        try:
            return self._call_cli(
                AGENTIC_SYSTEM_PROMPT, payload, session_id=session_id,
                extra_args=["--additional-mcp-config", "@" + cfg_path])
        finally:
            for p in (ev_path, cfg_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _call_cli(self, system_prompt: str, user_payload: str,
                  session_id: Optional[str] = None,
                  resume: bool = False,
                  extra_args: Optional[List[str]] = None) -> str:
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

    def _call_chat_completions(self, system_prompt: str, user_payload: str) -> str:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
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
