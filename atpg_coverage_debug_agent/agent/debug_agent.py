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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


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
# Agent configuration
# ---------------------------------------------------------------------------
@dataclass
class AgentConfig:
    """Configuration for the LLM backend.

    Attributes:
        base_url:    OpenAI-compatible base URL (e.g. ``https://host/v1``).
        model:       Model name to request.
        api_key:     Bearer token (kept in-session only; never persisted to disk).
        temperature: Sampling temperature.
        max_tokens:  Maximum completion tokens.
        max_faults:  Cap on coverage-loss faults serialised into the payload.
        timeout:     HTTP timeout in seconds.
    """

    base_url: str = ""
    model: str = "gpt-4"
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 4000
    max_faults: int = 200
    timeout: int = 120

    @property
    def configured(self) -> bool:
        """True when enough is set to attempt a live LLM call."""
        return bool(self.base_url.strip() and self.model.strip())


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------
def build_user_payload(report: Any, max_faults: int = 200) -> str:
    """Serialise an :class:`AnalysisReport` into a structured text payload.

    The structural analyser has already correlated faults to netlist objects;
    this function presents that evidence compactly so the LLM reasons over
    *observed structural facts* rather than re-deriving connectivity.

    Args:
        report:     A populated ``AnalysisReport``.
        max_faults: Maximum number of coverage-loss faults to include.

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
    if skill_results:
        lines.append("## Skill Findings (auxiliary structural skills)")
        for sr in skill_results:
            lines.append(f"### {sr.skill_id}: {sr.summary}")
            for f in getattr(sr, "findings", [])[:10]:
                lines.append(f"- {f.title} [{f.confidence}] — {f.description}")
        lines.append("")

    lines.append("## TASK")
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

    def run(self, report: Any) -> str:
        """Call the LLM and return its completion text.

        Raises:
            RuntimeError: if the endpoint is not configured or the call fails.
        """
        if not self.config.configured:
            raise RuntimeError(
                "No LLM endpoint configured. Set a base URL and model, or use "
                "'Build Prompt Only' to copy the prompt into your own chat model."
            )
        user_payload = self.build_prompt(report)
        return self._call_chat_completions(SYSTEM_PROMPT, user_payload)

    def run_with_prompt(self, system_prompt: str, user_payload: str) -> str:
        """Call the LLM with an explicit system + user prompt pair."""
        if not self.config.configured:
            raise RuntimeError("No LLM endpoint configured.")
        return self._call_chat_completions(system_prompt, user_payload)

    # -- internal ------------------------------------------------------------

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
