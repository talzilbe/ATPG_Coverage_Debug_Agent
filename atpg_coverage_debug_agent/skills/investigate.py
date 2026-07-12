"""Investigative, parameterised skills exposed as callable agent tools.

Unlike the bulk analysis skills, these are *on-demand query tools*: the agent
calls them with arguments to drill into specific faults, constraints, or
structural paths. Every one delegates to the deterministic query core in
:mod:`atpg_coverage_debug_agent.analysis.investigate`, so the exact same logic
backs both the HTTP tool-calling loop and the Copilot CLI MCP server.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from ..analysis import investigate
from .base import AnalysisContext, SkillBase, SkillResult
from .registry import register


class _InvestigativeSkill(SkillBase):
    """Base for on-demand tools backed by a single ``investigate`` function."""

    #: Tool name in ``investigate.TOOL_SPECS`` (also the skill_id).
    tool_name: str = ""
    default_enabled = True
    on_demand = True

    _TYPE_MAP = {"str": "str", "int": "int", "bool": "bool", "float": "float"}

    def parameters_schema(self) -> Dict[str, Dict[str, Any]]:
        spec = investigate.TOOL_SPECS.get(self.tool_name, {})
        schema: Dict[str, Dict[str, Any]] = {}
        for pname, pspec in spec.get("params", {}).items():
            entry: Dict[str, Any] = {
                "type": self._TYPE_MAP.get(pspec.get("type", "str"), "str"),
                "description": pspec.get("description", ""),
                "default": pspec.get("default", self._empty_default(pspec)),
            }
            schema[pname] = entry
        return schema

    @staticmethod
    def _empty_default(pspec: Dict[str, Any]) -> Any:
        t = pspec.get("type", "str")
        return {"int": 0, "float": 0.0, "bool": False}.get(t, "")

    def run(self, ctx: AnalysisContext) -> SkillResult:
        result = SkillResult(skill_id=self.skill_id)
        args = {name: self.get_param(name) for name in self.parameters_schema()}
        try:
            data = investigate.run_tool(
                self.tool_name, args,
                fault_results=ctx.fault_results,
                constraints=ctx.constraints,
                netlist=ctx.netlist,
                adjacency=getattr(ctx, "adjacency", None),
            )
        except Exception as exc:  # noqa: BLE001
            result.success = False
            result.add_error(f"{self.tool_name} failed: {exc}")
            result.summary = f"{self.tool_name} error: {exc}"
            return result

        if isinstance(data, dict) and data.get("error"):
            result.add_warning(str(data["error"]))
            result.summary = str(data["error"])
            return result

        # Surface the structured result both as a compact finding and as raw
        # JSON in a message so tool consumers get machine-readable output.
        result.add_info(json.dumps(data, indent=2, default=str))
        result.summary = self._summarize(data)
        self._add_findings(result, data)
        return result

    def _summarize(self, data: Dict[str, Any]) -> str:
        if "total_matched" in data:
            return f"{self.tool_name}: {data.get('total_matched', 0)} match(es)."
        if "found" in data:
            return (f"{self.tool_name}: path "
                    + ("found." if data.get("found") else "not found."))
        return f"{self.tool_name}: done."

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        """Default: no structured findings (raw JSON already attached)."""
        return


@register
class ListFaultsSkill(_InvestigativeSkill):
    skill_id = "list_faults"
    tool_name = "list_faults"
    display_name = "List Faults (query)"
    description = investigate.TOOL_SPECS["list_faults"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for row in data.get("faults", [])[:10]:
            result.add_finding(
                title=f"{row['fault_class']} {row['fault_object']}",
                description=(f"instance={row.get('instance')} "
                            f"root_cause={row.get('root_cause')}"),
                affected_objects=[row.get("instance") or row["fault_object"]],
                confidence=row.get("confidence", "medium"),
            )


@register
class FaultDetailSkill(_InvestigativeSkill):
    skill_id = "get_fault_detail"
    tool_name = "get_fault_detail"
    display_name = "Fault Detail (query)"
    description = investigate.TOOL_SPECS["get_fault_detail"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for row in data.get("faults", []):
            result.add_finding(
                title=f"Detail: {row['fault_object']}",
                description=(f"class={row['fault_class']} "
                            f"root_cause={row.get('root_cause')} "
                            f"ctrl={row.get('controllability_issue')} "
                            f"obsv={row.get('observability_issue')}"),
                evidence=list(row.get("evidence", []))[:8],
                affected_objects=[row.get("instance") or row["fault_object"]],
                confidence=row.get("confidence", "medium"),
                recommendation=row.get("recommended_step", ""),
            )


@register
class WhyBlockedSkill(_InvestigativeSkill):
    skill_id = "why_blocked"
    tool_name = "why_blocked"
    display_name = "Why Blocked (query)"
    description = investigate.TOOL_SPECS["why_blocked"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for row in data.get("faults", []):
            result.add_finding(
                title=f"{row['fault_object']}: {row['verdict']}",
                description=(f"root_cause={row.get('root_cause')} "
                            f"constraint_related={row.get('constraint_related')}"),
                evidence=list(row.get("observed_facts", []))[:6],
                affected_objects=[row.get("instance") or row["fault_object"]],
                confidence="high",
                recommendation=row.get("recommended_step", ""),
            )


@register
class ListConstraintsSkill(_InvestigativeSkill):
    skill_id = "list_constraints"
    tool_name = "list_constraints"
    display_name = "List Constraints (query)"
    description = investigate.TOOL_SPECS["list_constraints"]["description"]


@register
class TracePathSkill(_InvestigativeSkill):
    skill_id = "trace_path"
    tool_name = "trace_path"
    display_name = "Trace Path (query)"
    description = investigate.TOOL_SPECS["trace_path"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        if data.get("found"):
            result.add_finding(
                title=f"Path {data['from']} -> {data['to']} ({data['hops']} hops)",
                description=" -> ".join(data.get("path", [])),
                confidence="high",
            )
        elif "note" in data:
            result.add_finding(
                title="No structural path within depth bound",
                description=data["note"],
                confidence="medium",
            )
