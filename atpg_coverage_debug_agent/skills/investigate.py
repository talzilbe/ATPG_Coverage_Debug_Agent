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
                compare=getattr(ctx, "compare", None),
                triage=getattr(ctx, "triage", None),
                context=getattr(ctx, "context", None),
                design=getattr(ctx, "design", None),
                findings=getattr(ctx, "findings", None),
                fix_edits=getattr(ctx, "fix_edits", None),
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
class DiagnoseUnresolvedSkill(_InvestigativeSkill):
    skill_id = "diagnose_unresolved"
    tool_name = "diagnose_unresolved"
    display_name = "Diagnose Unresolved Mappings (query)"
    description = investigate.TOOL_SPECS["diagnose_unresolved"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for group in data.get("groups", []):
            result.add_finding(
                title=(f"{group['count']} unmapped fault(s): "
                       f"{group['cause']}"),
                description=group.get("meaning", ""),
                evidence=list(group.get("samples", [])),
                affected_objects=list(group.get("samples", [])),
                confidence="high",
            )


@register
class ScanStatusSkill(_InvestigativeSkill):
    skill_id = "scan_status"
    tool_name = "scan_status"
    display_name = "Scan Status (query)"
    description = investigate.TOOL_SPECS["scan_status"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        verdict = data.get("verdict", "unresolved")
        result.add_finding(
            title=f"{data.get('target')}: {verdict}",
            description=data.get("answer", ""),
            evidence=list(data.get("evidence", []))
            + list(data.get("blockers", [])),
            affected_objects=[data.get("instance") or data.get("target") or ""],
            confidence="high" if verdict != "unresolved" else "insufficient",
        )


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
class ListCategoryFaultsSkill(_InvestigativeSkill):
    skill_id = "list_category_faults"
    tool_name = "list_category_faults"
    display_name = "List Category Faults (query)"
    description = investigate.TOOL_SPECS["list_category_faults"]["description"]

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for row in data.get("faults", [])[:10]:
            result.add_finding(
                title=f"{row.get('dotted_class') or row['fault_class']} "
                      f"{row['fault_object']}",
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
class SuggestTestPointsSkill(_InvestigativeSkill):
    skill_id = "suggest_test_points"
    tool_name = "suggest_test_points"
    display_name = "Suggest Test Points (query)"
    description = investigate.TOOL_SPECS["suggest_test_points"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        return f"suggest_test_points: {data.get('total', 0)} suggestion(s)."

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for s in data.get("suggestions", [])[:10]:
            result.add_finding(
                title=f"[{s['kind']}] {s['instance']} (score {s['score']})",
                description=s["suggested_action"],
                evidence=[s["rationale"],
                          f"fan_in={s['fan_in']} fan_out={s['fan_out']}"],
                affected_objects=[s.get("instance") or s["fault_object"]],
                confidence="medium",
                recommendation=s["suggested_action"])


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


@register
class RegressionSummarySkill(_InvestigativeSkill):
    skill_id = "regression_summary"
    tool_name = "regression_summary"
    display_name = "Regression Summary (query)"
    description = investigate.TOOL_SPECS["regression_summary"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        c = data.get("counts", {})
        return (f"regression: +{c.get('regressed', 0)} regressed, "
                f"-{c.get('fixed', 0)} fixed, {c.get('changed', 0)} changed "
                f"(net {c.get('net_delta', 0)}).")


@register
class ListRegressedSkill(_InvestigativeSkill):
    skill_id = "list_regressed"
    tool_name = "list_regressed"
    display_name = "List Regressed (query)"
    description = investigate.TOOL_SPECS["list_regressed"]["description"]


@register
class ListFixedSkill(_InvestigativeSkill):
    skill_id = "list_fixed"
    tool_name = "list_fixed"
    display_name = "List Fixed (query)"
    description = investigate.TOOL_SPECS["list_fixed"]["description"]


@register
class ListChangedSkill(_InvestigativeSkill):
    skill_id = "list_changed"
    tool_name = "list_changed"
    display_name = "List Changed (query)"
    description = investigate.TOOL_SPECS["list_changed"]["description"]


@register
class CoverageTriageSkill(_InvestigativeSkill):
    skill_id = "coverage_triage"
    tool_name = "coverage_triage"
    display_name = "Coverage Triage (query)"
    description = investigate.TOOL_SPECS["coverage_triage"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        totals = data.get("totals", {})
        return (f"coverage_triage: {totals.get('coverage_loss', 0)} "
                f"coverage-loss fault(s) across "
                f"{len(data.get('categories', []))} categorie(s); "
                f"{len(data.get('selected', []))} selected for debug.")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for row in data.get("selected", []):
            result.add_finding(
                title=(f"{row['subclass']}: {row['count']} fault(s) "
                       f"({row['pct']:.2f}%)"),
                description=row.get("reason", ""),
                confidence="high",
                recommendation=(
                    f"Run recommend_fixes with subclass='{row['subclass']}' "
                    f"for concrete next steps."))


@register
class RecommendFixesSkill(_InvestigativeSkill):
    skill_id = "recommend_fixes"
    tool_name = "recommend_fixes"
    display_name = "Recommend Fixes (query)"
    description = investigate.TOOL_SPECS["recommend_fixes"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        return f"recommend_fixes: {data.get('total', 0)} proposal(s)."

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for rec in data.get("recommendations", [])[:10]:
            evidence = list(rec.get("evidence", []))
            if rec.get("requires_measurement"):
                evidence.append(
                    "Benefit must be measured by an ATPG re-run; no gain is "
                    "predicted here.")
            result.add_finding(
                title=f"[{rec['subclass']}] {rec['title']}",
                description=rec.get("rationale", ""),
                evidence=evidence,
                confidence=rec.get("confidence", "medium"),
                recommendation=rec.get("expected_effect", ""))


@register
class ExplainSubclassSkill(_InvestigativeSkill):
    skill_id = "explain_subclass"
    tool_name = "explain_subclass"
    display_name = "Explain Subclass (query)"
    description = investigate.TOOL_SPECS["explain_subclass"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if not data.get("known"):
            return f"explain_subclass: '{data.get('subclass')}' is not catalogued."
        return f"explain_subclass: {data['matched']} — {data['title']}."


@register
class ListClustersSkill(_InvestigativeSkill):
    skill_id = "list_clusters"
    tool_name = "list_clusters"
    display_name = "List Clusters (query)"
    description = investigate.TOOL_SPECS["list_clusters"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        categories = data.get("categories", [])
        return (f"list_clusters: hierarchy hotspots for "
                f"{len(categories)} categorie(s).")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for entry in data.get("categories", []):
            clusters = entry.get("clusters", [])
            if not clusters:
                continue
            top = clusters[0]
            result.add_finding(
                title=(f"[{entry['subclass']}] {top['pct']:.1f}% under "
                       f"{top['prefix']}"),
                description=(f"{top['count']} of {entry.get('total_faults', 0)} "
                             f"fault(s), stuck-at split "
                             f"{top['sa0']}/{top['sa1']}."),
                evidence=list(top.get("samples", [])),
                affected_objects=[top["prefix"]],
                confidence="medium",
                recommendation=("Investigate this hierarchy first. The prefix "
                                "shows where the faults are, not why."))


@register
class ListBlockingSourcesSkill(_InvestigativeSkill):
    skill_id = "list_blocking_sources"
    tool_name = "list_blocking_sources"
    display_name = "List Blocking Sources (query)"
    description = investigate.TOOL_SPECS["list_blocking_sources"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        categories = data.get("categories", [])
        return (f"list_blocking_sources: traced "
                f"{len(categories)} categorie(s).")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for entry in data.get("categories", []):
            evidence = [entry.get("caveat", "")]
            objects = []
            for source in entry.get("tie_sources", [])[:3]:
                objects.append(source["driver"])
                evidence.append(
                    f"{source['driver']} [{source['cell_type']}] → "
                    f"{source['count']} fault(s)")
            for source in entry.get("constraint_sources", [])[:3]:
                objects.append(source["signal"])
                evidence.append(
                    f"{source['signal']} = {source['value'] or '?'} → "
                    f"{source['count']} fault(s)")
            result.add_finding(
                title=(f"[{entry['subclass']}] {entry['verdict']} "
                       f"({entry['attributed']}/{entry['analysed']} traced)"),
                description=entry.get("note", ""),
                evidence=[e for e in evidence if e],
                affected_objects=objects,
                confidence="medium",
                recommendation=entry.get("note", ""))


@register
class ProfileFaultSitesSkill(_InvestigativeSkill):
    skill_id = "profile_fault_sites"
    tool_name = "profile_fault_sites"
    display_name = "Profile Fault Sites (query)"
    description = investigate.TOOL_SPECS["profile_fault_sites"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        categories = data.get("categories", [])
        return f"profile_fault_sites: profiled {len(categories)} categorie(s)."

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for entry in data.get("categories", []):
            if not entry.get("dominant"):
                continue
            top = next((s for s in entry.get("signatures", [])
                        if s["signature"] == entry["dominant"]), None)
            result.add_finding(
                title=(f"[{entry['subclass']}] {entry['dominant_label']} "
                       f"({entry['dominant_share']:.0%} of "
                       f"{entry['profiled']} site(s))"),
                description=entry.get("note", ""),
                evidence=(list(top.get("samples", [])) if top else [])
                + [entry.get("caveat", "")],
                confidence="high" if entry.get("consensus") else "medium",
                recommendation=(top or {}).get("meaning", ""))


@register
class ReportContextSkill(_InvestigativeSkill):
    skill_id = "report_context"
    tool_name = "report_context"
    display_name = "Report Context (query)"
    description = investigate.TOOL_SPECS["report_context"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        evidence = data.get("evidence") or {}
        share = evidence.get("mapped_share")
        if share is None:
            return "report_context: returned."
        return (f"report_context: {share:.0%} of coverage-loss faults mapped "
                f"onto the netlist.")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        evidence = data.get("evidence") or {}
        if evidence.get("never_mapped"):
            result.add_finding(
                title=(f"{evidence['never_mapped']} coverage-loss fault(s) "
                       "never mapped onto the netlist"),
                description=("Their connectivity is unknown, not zero. Any "
                             "figure computed over them is weaker than it "
                             "looks."),
                evidence=[f"{cause}: {n}" for cause, n
                          in (evidence.get("why_unmapped") or {}).items()][:6],
                confidence="high")
        waivers = data.get("waivers")
        if waivers:
            result.add_finding(
                title=(f"An analyst waived {waivers.get('removed_count', 0)} "
                       "fault(s)"),
                description=waivers.get("caveat", ""),
                evidence=[waivers.get("note", "")] if waivers.get("note") else [],
                confidence="high")


@register
class VisualizerCommandsSkill(_InvestigativeSkill):
    skill_id = "visualizer_commands"
    tool_name = "visualizer_commands"
    display_name = "Visualizer Commands (query)"
    description = investigate.TOOL_SPECS["visualizer_commands"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return "visualizer_commands: no viewer session is configured."
        steps = data.get("steps") or []
        return (f"visualizer_commands: {len(steps)} command(s) to reopen this "
                f"design in the viewer.")


@register
class ReportInsufficientEvidenceSkill(_InvestigativeSkill):
    skill_id = "report_insufficient_evidence"
    tool_name = "report_insufficient_evidence"
    display_name = "Report Insufficient Evidence (query)"
    description = investigate.TOOL_SPECS[
        "report_insufficient_evidence"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return f"report_insufficient_evidence: {data['error']}"
        return ("report_insufficient_evidence: recorded — the evidence does "
                "not settle this question.")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        if data.get("error"):
            return
        result.add_finding(
            title=f"Not determined: {data.get('question', '')}",
            description=data.get("missing", ""),
            recommendation=data.get("would_settle_it", ""),
            confidence="insufficient")


@register
class ReportHandoffGapSkill(_InvestigativeSkill):
    skill_id = "report_handoff_gap"
    tool_name = "report_handoff_gap"
    display_name = "Report Hand-off Gap (query)"
    description = investigate.TOOL_SPECS["report_handoff_gap"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return f"report_handoff_gap: {data['error']}"
        return ("report_handoff_gap: recorded — the figures handed over do "
                "not reconcile with each other.")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        if data.get("error"):
            return
        result.add_finding(
            title=f"Hand-off inconsistency in {data.get('where') or 'the payload'}",
            description=data.get("observed", ""),
            recommendation=data.get("next_step", ""),
            evidence=[e for e in (data.get("expected", ""),) if e],
            confidence="high")


@register
class VerifyPathsSkill(_InvestigativeSkill):
    skill_id = "verify_paths"
    tool_name = "verify_paths"
    display_name = "Verify Paths (query)"
    description = investigate.TOOL_SPECS["verify_paths"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        bad = [c for c in data.get("checked", []) if not c["ok"]]
        return (f"verify_paths: {len(bad)} unverifiable path(s), "
                f"{len(data.get('text_issues', []))} text issue(s).")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for row in data.get("checked", []):
            if row["ok"]:
                continue
            result.add_finding(
                title=f"Unverifiable path: {row['path']}",
                description=("This path does not appear in the fault list, "
                             "constraint file or netlist."),
                affected_objects=[row["path"]],
                confidence="high",
                recommendation=("Quote the path verbatim from a source "
                                "artefact instead."))


@register
class ListOpenQuestionsSkill(_InvestigativeSkill):
    skill_id = "list_open_questions"
    tool_name = "list_open_questions"
    display_name = "List Open Questions (query)"
    description = investigate.TOOL_SPECS["list_open_questions"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return f"list_open_questions: {data['error']}"
        return (f"list_open_questions: {data.get('total', 0)} question(s) "
                "the offline analysis left open.")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for q in data.get("questions", []):
            result.add_finding(
                title=f"[{q.get('priority')}] {q.get('subject')}: "
                      f"{q.get('question')}",
                description=q.get("why", ""),
                recommendation="Settle with: " + ", ".join(
                    q.get("suggested_tools", [])),
                confidence="high")


@register
class ClassificationCrosscheckSkill(_InvestigativeSkill):
    skill_id = "classification_crosscheck"
    tool_name = "classification_crosscheck"
    display_name = "Subclass vs Root-Cause Cross-check (query)"
    description = investigate.TOOL_SPECS["classification_crosscheck"][
        "description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return f"classification_crosscheck: {data['error']}"
        t = data.get("totals", {})
        return (f"classification_crosscheck: {t.get('agree', 0)} agree, "
                f"{t.get('disagree', 0)} disagree, "
                f"{t.get('unconfirmed', 0)} unconfirmed.")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        for lead in data.get("leads", []):
            result.add_finding(
                title=(f"{lead.get('verdict')}: {lead.get('count')} "
                       f"{lead.get('subclass')} fault(s) resolve to "
                       f"{lead.get('root_cause')}"),
                description=lead.get("why_it_matters", ""),
                evidence=list(lead.get("samples", [])),
                affected_objects=list(lead.get("samples", [])),
                confidence="medium")


@register
class RecordFindingSkill(_InvestigativeSkill):
    skill_id = "record_finding"
    tool_name = "record_finding"
    display_name = "Record Finding (action)"
    description = investigate.TOOL_SPECS["record_finding"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return f"record_finding: {data['error']}"
        f = data.get("finding", {})
        return (f"record_finding: {f.get('kind')} on {f.get('subject')} "
                f"recorded ({data.get('count', 0)} so far).")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        if data.get("error"):
            return
        f = data.get("finding", {})
        result.add_finding(
            title=f"{f.get('kind')}: {f.get('subject')} [{f.get('field')}]",
            description=(f"offline: {f.get('offline_value') or '-'} | agent: "
                         f"{f.get('agent_value') or '-'}"),
            evidence=[f.get("evidence", "")] if f.get("evidence") else [],
            affected_objects=[f.get("subject", "")],
            confidence=f.get("confidence", "medium"))


@register
class ProposeFixSkill(_InvestigativeSkill):
    skill_id = "propose_fix"
    tool_name = "propose_fix"
    display_name = "Propose Fix (action)"
    description = investigate.TOOL_SPECS["propose_fix"]["description"]

    def _summarize(self, data: Dict[str, Any]) -> str:
        if data.get("error"):
            return f"propose_fix: {data['error']}"
        e = data.get("edit", {})
        return (f"propose_fix: {e.get('action')} on {e.get('subclass')} "
                f"recorded ({data.get('edits_so_far', 0)} edit(s) so far).")

    def _add_findings(self, result: SkillResult, data: Dict[str, Any]) -> None:
        if data.get("error"):
            return
        e = data.get("edit", {})
        result.add_finding(
            title=f"fix plan {e.get('action')}: {e.get('subclass')} "
                  f"{e.get('title') or ''}".strip(),
            description=e.get("rationale") or e.get("note") or "",
            evidence=[e.get("evidence", "")] if e.get("evidence") else [],
            affected_objects=[e.get("subclass", "")],
            recommendation="\n".join(e.get("commands") or []),
            confidence=e.get("confidence", "medium"))

