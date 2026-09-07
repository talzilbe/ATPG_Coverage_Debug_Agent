"""Render an :class:`AnalysisReport` as Markdown."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

from ..analysis.census import build_census
from ..models import AnalysisReport, FaultAnalysisResult

logger = logging.getLogger(__name__)


def _fmt_list(items: List[str], limit: int = 6) -> str:
    if not items:
        return "_none_"
    shown = items[:limit]
    suffix = "" if len(items) <= limit else f" (+{len(items) - limit} more)"
    return ", ".join(shown) + suffix


def _fault_row(r: FaultAnalysisResult) -> str:
    # NULL (not 0) and 'unknown' (not 'no') whenever the object never mapped.
    fan_in = r.fan_in_count
    fan_out = r.fan_out_count
    return "| {obj} | {cls} | {mapped} | {conf} | {inst} | {cell} | {fi} | "\
        "{fo} | {ctrl} | {obsv} | {con} | {scan} | {rc} |".format(
            obj=r.fault.fault_object,
            cls=r.fault.fault_class.value,
            mapped=r.mapping.instance_name or "—",
            conf=r.mapping.confidence.value,
            inst=r.instance_name or "—",
            cell=r.cell_type or "—",
            fi="NULL" if fan_in is None else fan_in,
            fo="NULL" if fan_out is None else fan_out,
            ctrl="yes" if r.controllability_issue else "no",
            obsv="yes" if r.observability_issue else "no",
            con="yes" if r.constraint_related else "no",
            scan=r.scan_boundary_state,
            rc=r.root_cause.value,
        )


def _metrics_section(report: AnalysisReport) -> List[str]:
    """Render the three Tessent coverage metrics with their inputs.

    Percentages are shown next to the counts they come from so any figure can
    be re-derived by hand, and a metric that is undefined for this population
    is printed as ``n/a`` rather than as a fabricated number.
    """
    stats = getattr(report, "statistics", None)
    if stats is None or not hasattr(stats, "metrics"):
        return []
    m = stats.metrics()
    roles = m["roles"]

    def _fmt(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:.4f}%"

    lines = ["### Coverage metrics", ""]
    lines.append(f"> Possibly-detected credit (`posdet_credit`) = "
                 f"**{m['posdet_credit']}**. Numerator is "
                 f"`DT + posdet_credit x PD` = {m['detected_credit']}.")
    header = getattr(report, "fault_list_header", None)
    if header is not None:
        lines.append(f"> Fault population is **{header.collapsing_label}** "
                     f"(model(s): {', '.join(header.fault_models) or 'n/a'}). "
                     f"A collapsed and an uncollapsed census are not "
                     f"comparable.")
    lines.append("")
    lines.append("| Metric | Value | Formula | Numeric substitution |")
    lines.append("| --- | --- | --- | --- |")
    # Every figure carries the arithmetic that produced it. UD below is the
    # FULL undetectable population from the role map; reading it as a single
    # class is what once put the headline metric ~2 points out.
    formulas = m.get("formulas", {})
    for key, label in (("test_coverage", "Test coverage"),
                       ("fault_coverage", "Fault coverage"),
                       ("atpg_effectiveness", "ATPG effectiveness")):
        spec = formulas.get(key, {})
        lines.append(f"| {label} | {_fmt(m[key])} | "
                     f"`{spec.get('formula', '')}` | "
                     f"`{spec.get('substitution', '')}` |")
    lines.append("")
    lines.append(f"> {m.get('ud_definition', '')}")
    lines.append(f"> {m.get('basis', '')}")
    lines.append("")
    lines.append("| Role | Meaning | Faults |")
    lines.append("| --- | --- | --- |")
    meanings = {
        "DT": "detected",
        "PD": "possibly detected (partial credit)",
        "UD": "undetectable (removed from the test-coverage denominator)",
        "AU": "ATPG untestable (stays in the denominator)",
        "ND": "not detected — coverage loss",
    }
    for role, meaning in meanings.items():
        lines.append(f"| {role} | {meaning} | {roles.get(role, 0)} |")
    lines.append(f"| FU | total fault population | {m['total_faults']} |")
    if m["unrecognised"]:
        lines.append(f"| (unrecognised) | class not in the configured role "
                     f"map — excluded from every metric | "
                     f"{m['unrecognised']} |")
    lines.append("")
    if not m["census_balances"]:
        lines.append("> **The census does not reconcile with the parsed "
                     "record count.** Treat every figure above as unusable.")
        lines.append("")
    return lines


def _census_section(report: AnalysisReport) -> List[str]:
    """Render the complete fault census (S2b), grouped by coverage role.

    Always emitted. Every class present is listed under the role it was
    counted as, and the section closes with the sum check, so a reader never
    has to work out whether what they are looking at is the whole population.
    """
    census = build_census(report)
    lines = ["## S2b. Complete Fault Census", ""]
    if not census.entries:
        lines.append("_No faults were parsed, so there is no census._")
        lines.append("")
        return lines

    lines.append(
        f"Every one of the {census.class_count} fault class(es) in the fault "
        f"list, grouped by coverage role. This is the complete census: no "
        f"class is filtered out, including the ones that are not debug "
        f"targets. Any other class listing in this report is a subset and "
        f"says so.")
    lines.append("")

    for role in census.roles:
        lines.append(f"### {role.role} — {role.label} ({role.count} faults, "
                     f"{100.0 * role.count / census.total_faults if census.total_faults else 0.0:.2f}%)")
        lines.append("")
        lines.append(f"> {role.scope}")
        lines.append("")
        lines.append("| Family | Subclass | Count | % of all | sa0 | sa1 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for family in role.families:
            for entry in family.entries:
                lines.append(
                    f"| {family.family} | {entry.subclass} | {entry.count} | "
                    f"{entry.pct:.2f}% | {entry.sa0} | {entry.sa1} |")
        lines.append("")

    rec = census.reconciliation()
    lines.append("### Census self-check")
    lines.append("")
    lines.append(f"- Sum of all {rec['classes']} class counts: "
                 f"**{rec['sum_of_class_counts']}**")
    lines.append(f"- Total faults analysed: **{rec['total_faults']}**")
    if rec["reconciles"]:
        lines.append("- Delta: **0** — the census accounts for every parsed "
                     "fault.")
    else:
        lines.append(f"- Delta: **{rec['delta']}** — **THE CENSUS DOES NOT "
                     f"RECONCILE.** No figure derived from it is "
                     f"trustworthy until this is attributed. Do not assign "
                     f"the delta to a category; it does not have one.")
    if rec["unclassified_tokens"]:
        lines.append(f"- Unclassified class token(s): "
                     f"{', '.join(rec['unclassified_tokens'])} — not in the "
                     f"configured class-role map, so they contribute to no "
                     f"coverage metric.")
    lines.append("")
    return lines


def _input_quality_section(report: AnalysisReport) -> List[str]:
    """Report how completely the inputs were understood.

    Kept separate from the findings on purpose. A directive that failed to
    parse and a design with no constraints look identical in a report that
    only prints results, and presenting the first as the second turns a tool
    limitation into a claim about the hardware.
    """
    header = getattr(report, "fault_list_header", None)
    diagnostics = getattr(report, "class_diagnostics", None)
    constraint_status = getattr(report, "constraint_diagnostics", None)
    if header is None and diagnostics is None and not constraint_status:
        return []

    lines = ["## Input Quality", ""]

    if header is not None:
        lines.append("### Fault list as declared")
        lines.append("")
        lines.append("| Property | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Format | {header.file_format} |")
        lines.append(f"| Compression (from magic bytes) | {header.compression} |")
        lines.append(f"| Declared version | {header.version or 'not declared'} |")
        lines.append(f"| Fault collapsing | {header.collapsing_label} |")
        lines.append(f"| Fault model(s) | "
                     f"{', '.join(header.fault_models) or 'not declared'} |")
        lines.append(f"| Declared column order | "
                     f"{', '.join(header.format_fields) or 'not declared'} |")
        lines.append("")
        if len(header.per_model_counts) > 1:
            lines.append("#### Census per fault model")
            lines.append("")
            for model, counts in header.per_model_counts.items():
                total = sum(counts.values())
                lines.append(f"- **{model}**: {total} fault(s) across "
                             f"{len(counts)} class(es)")
            lines.append("")

    if diagnostics is not None and getattr(diagnostics, "tokens", None):
        lines.append("### Unrecognised fault classes")
        lines.append("")
        lines.append(f"> {diagnostics.count} of {diagnostics.total} record(s) "
                     f"({diagnostics.pct:.4f}%) carried a class the "
                     f"configuration does not describe. They are excluded "
                     f"from every coverage metric and are listed here so they "
                     f"can be inspected and the class map extended.")
        lines.append("")
        lines.append("| Class token | Records | First line | Sample |")
        lines.append("| --- | --- | --- | --- |")
        for tok in diagnostics.tokens:
            sample = (tok.samples[0] if tok.samples else "").replace("|", "\\|")
            lines.append(f"| `{tok.token}` | {tok.count} | "
                         f"{tok.first_line or '—'} | `{sample}` |")
        lines.append("")

    if constraint_status:
        total = constraint_status.get("directives", 0)
        unresolved = constraint_status.get("unresolved", 0)
        lines.append("### Constraint file parsing")
        lines.append("")
        lines.append("| Property | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Files read (includes followed) | "
                     f"{len(constraint_status.get('files') or [])} |")
        lines.append(f"| Directives recognised | "
                     f"{constraint_status.get('resolved', 0)} of {total} |")
        lines.append(f"| Directives NOT evaluated | {unresolved} |")
        lines.append(f"| Objects resolved from collections | "
                     f"{constraint_status.get('expanded_objects', 0)} |")
        lines.append("")
        if unresolved:
            lines.append(f"> **{unresolved} directive(s) were recognised but "
                         f"could not be evaluated.** While that is non-zero, "
                         f"a fault reported with no constraint hit is **not** "
                         f"proven unconstrained — the constraint file was "
                         f"only partly understood.")
            lines.append("")
        unrecognised = constraint_status.get("unrecognised") or {}
        for tok in (unrecognised.get("tokens") or []):
            lines.append(f"- Unknown directive `{tok['token']}` on "
                         f"{tok['count']} line(s); add it to "
                         f"`constraint_directives` in the analysis config.")
        if unrecognised.get("tokens"):
            lines.append("")

    return lines


def _evidence_section(report: AnalysisReport) -> List[str]:
    """Report how much of the coverage loss rests on evidence actually read.

    Unmapped rows and tie-driven faults are separated out before any ranking
    is presented: the first carry no evidence at all, the second are
    undetectable by construction. Leaving either inside the general loss total
    is what produces a confident and wrong priority list.
    """
    s = report.summary
    loss = s.coverage_loss_count or 0
    if not loss:
        return []

    lines = ["## Evidence Quality", ""]
    lines.append("| Basis | Faults | % of loss |")
    lines.append("| --- | --- | --- |")

    def _pct(n: int) -> str:
        return f"{100.0 * n / loss:.1f}%" if loss else "-"

    lines.append(f"| Mapped onto the netlist | {s.mapped_count} | "
                 f"{_pct(s.mapped_count)} |")
    lines.append(f"| Not mapped (connectivity **unknown**, not zero) | "
                 f"{s.unmapped_count} | {_pct(s.unmapped_count)} |")
    lines.append(f"| Held at a hard constant (tied, expected) | "
                 f"{s.tied_constant_count} | {_pct(s.tied_constant_count)} |")
    lines.append(f"| **Actionable coverage loss** | "
                 f"**{s.actionable_loss_count}** | "
                 f"**{_pct(s.actionable_loss_count)}** |")
    lines.append("")

    scan = dict(s.scan_evidence_counts or {})
    if scan:
        lines.append("### Scan status of the fault sites")
        lines.append("")
        lines.append("> Read from each instantiation's pin list. A cell counts "
                     "as scan only when a dedicated scan-data input **and** a "
                     "shift-enable pin were both read. `unknown` means no "
                     "instantiation was read — it is never reported as "
                     "non-scan.")
        lines.append("")
        lines.append("| Verdict | Faults |")
        lines.append("| --- | --- |")
        for key in ("scan", "non_scan", "unknown"):
            if key in scan:
                lines.append(f"| {key} | {scan[key]} |")
        lines.append("")

    causes = dict(s.unresolved_causes or {})
    if causes:
        lines.append("### Why faults did not map")
        lines.append("")
        lines.append("| Cause | Faults |")
        lines.append("| --- | --- |")
        for cause, count in sorted(causes.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {cause} | {count} |")
        lines.append("")

    ties = _tie_drivers(report)
    if ties:
        lines.append("### Constant drivers holding fault sites")
        lines.append("")
        lines.append("> Resolved across hierarchy feedthrough ports. These "
                     "faults are **expected and non-actionable**: a stuck-at "
                     "fault on a constant pin is undetectable whatever the "
                     "scan architecture.")
        lines.append("")
        lines.append("| Tie instance | Cell type | Value | Levels away | Faults |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, tie, count in ties:
            lines.append(f"| {name} | {tie.get('cell_type', '')} | "
                         f"{tie.get('value') or '?'} | "
                         f"{tie.get('levels', 0)} | {count} |")
        lines.append("")

    if s.unmapped_count:
        lines.append(f"> **{s.unmapped_count} coverage-loss fault(s) never "
                     f"mapped onto the netlist.** Their fan-in, fan-out and "
                     f"scan status are unknown and are rendered as `NULL` / "
                     f"`unknown`, never as `0` / `no`. No root cause on those "
                     f"sites is provable, and they must not be counted "
                     f"towards an observability or scan-boundary conclusion.")
        lines.append("")
    return lines


def _tie_drivers(report: AnalysisReport, limit: int = 10) -> List[tuple]:
    """Rank the tie cells holding the most fault sites at a constant."""
    counter: Counter = Counter()
    detail: dict = {}
    for r in report.fault_results:
        tie = getattr(r, "tie_driver", None)
        if not tie:
            continue
        key = tie.get("instance") or "(unnamed)"
        counter[key] += 1
        detail.setdefault(key, tie)
    return [(name, detail[name], count)
            for name, count in counter.most_common(limit)]


def _structural_profiles(selected: List) -> List[str]:
    """Render why each aborted category was structurally hard to test."""
    profiled = [c for c in selected if getattr(c, "reachability", None)
                and c.reachability.profiled]
    if not profiled:
        return []

    lines = ["### Why these faults were hard to test", ""]
    lines.append("> Estimated by walking the netlist. A narrow bottleneck and "
                 "a reconvergent cone need **opposite** fixes — more abort "
                 "budget helps the first and is wasted on the second — so "
                 "confirm the signature before acting.")
    lines.append("")
    for cat in profiled:
        prof = cat.reachability
        lines.append(f"**{cat.subclass_id}** — {prof.profiled} of "
                     f"{prof.analysed} site(s) profiled; dominant signature "
                     f"`{prof.dominant}` ({prof.dominant_share:.0%})")
        lines.append("")
        lines.append(prof.note)
        lines.append("")
        lines.append("| Signature | Sites | What it means |")
        lines.append("| --- | --- | --- |")
        for row in prof.as_dict()["signatures"]:
            lines.append(f"| {row['label']} | {row['count']} | "
                         f"{row['meaning']} |")
        lines.append("")
    return lines


def _blocking_sources(selected: List) -> List[str]:
    """Render what was traced to be blocking each attributable category.

    Hard tie cells are deliberately omitted: the Evidence Quality section
    already ranks them, resolved across hierarchy feedthrough ports and with
    the constant value, which is strictly better evidence than this bounded
    module-local cone walk. What remains is what that section cannot tell you
    -- drivers that are constant in practice but could be changed.
    """
    entries = []
    for cat in selected:
        att = getattr(cat, "attribution", None)
        if att is None or not att.attributed:
            continue
        changeable = [s for s in att.tie_sources if s.kind != "tie_cell"]
        if changeable or att.constraint_sources:
            entries.append((cat, att, changeable))
    if not entries:
        return []

    lines = ["### What is blocking these faults", ""]
    lines.append("> Derived by tracing fan-in cones through the netlist. This "
                 "is a structural **estimate** of the blocking source, not "
                 "the ATPG tool's own attribution — confirm before acting. "
                 "Hard tie cells are excluded here; they are listed under "
                 "Evidence Quality with a better trace.")
    lines.append("")
    for cat, att, changeable in entries:
        lines.append(f"**{cat.subclass_id}** — {att.attributed} of "
                     f"{att.analysed} fault(s) traced "
                     f"({att.coverage:.0%}), verdict `{att.verdict}`")
        lines.append("")
        lines.append(f"{att.note}")
        lines.append("")
        if changeable:
            lines.append("| Driver held constant | Cell type | Holding value | "
                         "Kind | Reprogrammable? | Faults |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for src in changeable[:5]:
                lines.append(f"| `{src.driver}` | {src.cell_type or '—'} | "
                             f"{src.tie_value or '?'} | {src.kind} | "
                             f"{'yes' if src.is_configurable else 'no'} | "
                             f"{src.count} |")
            lines.append("")
        if att.constraint_sources:
            lines.append("| Constrained signal | Kind | Value | Faults |")
            lines.append("| --- | --- | --- | --- |")
            for src in att.constraint_sources[:5]:
                lines.append(f"| `{src.signal}` | {src.kind or '—'} | "
                             f"{src.value or '?'} | {src.count} |")
            lines.append("")
    return lines


def _hotspot_tables(selected: List) -> List[str]:
    """Render the hierarchy hotspots for each selected category."""
    with_clusters = [c for c in selected
                     if getattr(c, "clusters", None)
                     and c.clusters.clusters]
    if not with_clusters:
        return []

    lines = ["### Where the loss concentrates", ""]
    lines.append("> A dominant prefix shows **where** to look. It is not a "
                 "root cause. Sample paths are verbatim and can be pasted "
                 "into a tool unmodified.")
    lines.append("")
    for cat in with_clusters:
        report = cat.clusters
        lines.append(f"**{cat.subclass_id}** — {report.total_faults} fault(s), "
                     f"{len(report.clusters)} cluster(s) at depth "
                     f"{report.depth} ({report.depth_note})")
        lines.append("")
        lines.append("| Hierarchy prefix | Faults | % | sa0 | sa1 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for cluster in report.clusters[:5]:
            lines.append(f"| `{cluster.prefix}` | {cluster.count} | "
                         f"{cluster.pct:.1f}% | {cluster.sa0} | "
                         f"{cluster.sa1} |")
        lines.append("")
        top = report.top
        if top is not None and top.samples:
            lines.append(f"Sample faults from `{top.prefix}`:")
            lines.append("")
            for sample in top.samples:
                lines.append(f"- `{sample}`")
            lines.append("")
    return lines


def _dumps_by_subclass(dumps: Optional[Sequence[Any]]) -> Dict[str, Any]:
    """Index category dumps by their subclass id for quick lookup."""
    return {d.subclass_id: d for d in (dumps or [])}


def _dump_links(dump: Any) -> str:
    """Render the report-relative links to one category's fault files."""
    parts = []
    if dump.csv_href:
        parts.append(f"[CSV]({dump.csv_href})")
    if dump.json_href:
        parts.append(f"[JSON]({dump.json_href})")
    if not parts:
        return ""
    suffix = ""
    if dump.truncated:
        suffix = (" — the JSON is capped; the CSV holds every fault")
    return (f"   - _All {dump.dumped_count} faults in this category:_ "
            f"{' · '.join(parts)}{suffix}")


def _triage_section(report: AnalysisReport,
                    dumps: Optional[Sequence[Any]] = None) -> List[str]:
    """Render the derived-statistics triage and the ranked fix plan.

    Args:
        report: The analysed report.
        dumps: Optional :class:`~.category_dump.CategoryDump` objects. When
            given, each selected category gains a link to the file holding its
            faults. Omitted for a pure in-memory render, where there are no
            files to point at.

    Returns an empty list when the report predates this section (for example a
    session file saved by an older version), so rendering stays backwards
    compatible.
    """
    stats = report.statistics
    if stats is None:
        return []

    lines: List[str] = []
    lines.append("## Coverage Triage")
    lines.append("")
    lines.append(f"- **Detected:** {stats.detected_count} "
                 f"({stats.detected_pct:.2f}% of the fault list)")
    lines.append(f"- **Coverage loss:** {stats.loss_count} "
                 f"({stats.loss_pct:.2f}%)")
    lines.append("")
    lines.append("> These figures are aggregated from the fault list. They are "
                 "not the tool's test-coverage number, which also accounts for "
                 "fault collapsing and untestable-fault credit.")
    lines.append("")

    loss_stats = stats.loss_stats
    if loss_stats:
        lines.append("### Coverage-loss categories")
        lines.append("")
        lines.append(
            f"> **This is a subset, not the census.** It lists the "
            f"{len(loss_stats)} debuggable coverage-loss categorie(s) only; "
            f"detected, possibly-detected and undetectable classes are "
            f"excluded by design, so these counts do not sum to the "
            f"{stats.total_faults} fault(s) analysed. The complete census is "
            f"in S2b.")
        lines.append("")
        lines.append("| Category | Faults | % of all | sa0 | sa1 | Imbalance |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for st in loss_stats:
            lines.append(
                f"| {st.subclass_id} | {st.count} | {st.pct:.2f}% | "
                f"{st.sa0} | {st.sa1} | {st.sa_asymmetry:.2f} |")
        lines.append("")

    selected = report.selected_categories or []
    if selected:
        by_subclass = _dumps_by_subclass(dumps)
        lines.append("### Selected for investigation")
        lines.append("")
        for cat in selected:
            info = cat.stat.info
            title = f" — {info.title}" if info else ""
            lines.append(f"{cat.rank}. **{cat.subclass_id}**{title}: "
                         f"{cat.reason}")
            if info:
                lines.append(f"   - {info.meaning}")
            verdict = getattr(cat, "verdict", None)
            if verdict is not None:
                lines.append(f"   - _Worth acting on:_ **{verdict.actionable}** "
                             f"({verdict.confidence.value} confidence) — "
                             f"{verdict.reason}")
                if verdict.patterns:
                    lines.append(f"   - _Pattern:_ "
                                 f"{', '.join(verdict.patterns)}")
            dump = by_subclass.get(cat.subclass_id)
            if dump is not None:
                link_line = _dump_links(dump)
                if link_line:
                    lines.append(link_line)
        lines.append("")

        lines.extend(_hotspot_tables(selected))
        lines.extend(_blocking_sources(selected))
        lines.extend(_structural_profiles(selected))

    recommendations = report.recommendations or []
    if not recommendations:
        return lines

    lines.append("## Fix Plan")
    lines.append("")
    lines.append("> Commands are provided for you to run in your own ATPG "
                 "session; this tool never executes them. Where an action is "
                 "marked as needing measurement, no coverage gain is predicted "
                 "— the re-run is what establishes the benefit.")
    lines.append("")
    for rec in recommendations:
        lines.append(f"### {rec.rank}. {rec.title}")
        lines.append("")
        lines.append(f"- **Category:** {rec.subclass_id} "
                     f"({rec.fault_count} faults, {rec.pct:.2f}%)")
        if rec.hotspot:
            lines.append(f"- **Concentrated under:** `{rec.hotspot}`")
        lines.append(f"- **Worth acting on:** {rec.actionable}")
        lines.append(f"- **Confidence:** {rec.confidence.value}")
        lines.append(f"- **Effort / risk:** {rec.fix.effort} / {rec.fix.risk}")
        lines.append(f"- **Why:** {rec.fix.rationale}")
        if rec.fix.preconditions:
            lines.append("- **Confirm first:**")
            for item in rec.fix.preconditions:
                lines.append(f"  - {item}")
        if rec.evidence:
            lines.append("- **Evidence:**")
            for item in rec.evidence:
                lines.append(f"  - {item}")
        if rec.fix.expected_effect:
            lines.append(f"- **Expected outcome:** {rec.fix.expected_effect}")
        if rec.caveats:
            lines.append("- **Caveats:**")
            for item in rec.caveats:
                lines.append(f"  - {item}")
        if rec.fix.commands:
            lines.append("")
            lines.append("```tcl")
            lines.extend(rec.fix.commands)
            lines.append("```")
        lines.append("")
    return lines


def render_markdown(report: AnalysisReport,
                    dumps: Optional[Sequence[Any]] = None) -> str:
    """Return a full Markdown document for *report*.

    Args:
        report: The analysed report.
        dumps: Optional per-category fault dumps to link from the triage
            section. ``None`` renders no links, so an in-memory render never
            points at files that do not exist.
    """
    s = report.summary
    lines: List[str] = []
    lines.append("# ATPG Coverage-Loss Debug Report")
    lines.append("")
    lines.append("> Generated by **atpg_coverage_debug_agent**. This is a "
                 "structural analysis. Conclusions are heuristic and carry "
                 "confidence/evidence — verify before acting.")
    lines.append("")

    # Executive summary
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- **Total faults analysed:** {s.total_faults}")
    lines.append(f"- **Coverage-loss faults (AU/UO/UC):** {s.coverage_loss_count}")
    lines.append("")
    lines.append("### Fault class counts (complete — every class present)")
    lines.append("")
    lines.append(f"> Coarse classes only; the dotted breakdown and the "
                 f"coverage role of each class are in S2b. Sums to "
                 f"{sum(s.class_counts.values())} of {s.total_faults} faults.")
    lines.append("")
    lines.append("| Class | Count |")
    lines.append("| --- | --- |")
    # Every class actually present is listed, largest first. A fixed list of
    # classes hides the ones the analyzer was not expecting, which is exactly
    # how five legitimate Tessent classes once went unreported.
    for cls, count in sorted(s.class_counts.items(),
                             key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {cls} | {count} |")
    lines.append("")

    lines.extend(_metrics_section(report))
    lines.extend(_census_section(report))

    lines.append("### Top root causes")
    lines.append("")
    if s.top_root_causes:
        lines.append("| Root cause | Count |")
        lines.append("| --- | --- |")
        for name, count in s.top_root_causes:
            lines.append(f"| {name} | {count} |")
    else:
        lines.append("_No coverage-loss faults._")
    lines.append("")

    lines.append("### Top affected instances (actionable loss only)")
    lines.append("")
    if s.top_instances:
        lines.append("> Tie-driven and unmapped faults are excluded, so this "
                     "ranks where real debug effort belongs. Those two "
                     "populations are counted under Evidence Quality above.")
        lines.append("")
        lines.append("| Instance | Faults |")
        lines.append("| --- | --- |")
        for name, count in s.top_instances:
            lines.append(f"| {name} | {count} |")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("### Top contributing constraints")
    lines.append("")
    if s.top_constraints:
        lines.append("| Constraint | Faults |")
        lines.append("| --- | --- |")
        for name, count in s.top_constraints:
            lines.append(f"| {name} | {count} |")
    else:
        lines.append("_none detected_")
    lines.append("")

    lines.extend(_evidence_section(report))
    lines.extend(_input_quality_section(report))
    lines.extend(_triage_section(report, dumps))

    # Per-fault table
    lines.append("## Per-Fault Detail")
    lines.append("")
    lines.append("| Fault Object | Class | Mapped | Conf | Instance | Cell | "
                 "Fan-in | Fan-out | Ctrl | Obsv | Constr | Scan | Root Cause |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | "
                 "--- | --- | --- | --- |")
    for r in report.fault_results:
        lines.append(_fault_row(r))
    lines.append("")

    # Evidence detail
    lines.append("## Evidence & Recommendations")
    lines.append("")
    for r in report.fault_results:
        lines.append(f"### {r.fault.fault_object} ({r.fault.fault_class.value})")
        lines.append("")
        lines.append(f"- **Root cause:** {r.root_cause.value}")
        lines.append(f"- **Mapping confidence:** {r.mapping.confidence.value}")
        lines.append(f"- **Observed facts:** {_fmt_list(r.observed_facts)}")
        lines.append(f"- **Inferred conclusions:** "
                     f"{_fmt_list(r.inferred_conclusions)}")
        lines.append(f"- **Evidence:** {_fmt_list(r.evidence)}")
        if r.mapping.candidates:
            lines.append(f"- **Candidate mappings:** "
                         f"{_fmt_list(r.mapping.candidates)}")
        lines.append(f"- **Recommended next step:** {r.recommended_step}")
        lines.append("")

    # Patterns
    lines.append("## Repeated Patterns")
    lines.append("")
    if report.pattern_groups:
        lines.append("| Kind | Key | Count | Sample faults |")
        lines.append("| --- | --- | --- | --- |")
        for g in report.pattern_groups:
            lines.append(f"| {g.kind} | {g.key} | {g.count} | "
                         f"{_fmt_list(g.sample_faults, 3)} |")
    else:
        lines.append("_No repeated patterns detected._")
    lines.append("")

    # Warnings
    lines.append("## Warnings & Limitations")
    lines.append("")
    if report.warnings:
        for w in report.warnings:
            lines.append(f"- {w}")
    else:
        lines.append("_No warnings._")
    lines.append("")

    # Skill Results
    if report.skill_results:
        lines.append("## Skill Results")
        lines.append("")
        for sr in report.skill_results:
            lines.append(f"### {sr.skill_id}")
            lines.append("")
            status = "✓ Success" if sr.success else "✗ Failed"
            lines.append(f"**Status:** {status}")
            lines.append(f"**Summary:** {sr.summary}")
            lines.append("")

            if sr.findings:
                lines.append("#### Findings")
                lines.append("")
                for finding in sr.findings:
                    lines.append(f"- **{finding.title}** [{finding.confidence}]")
                    lines.append(f"  {finding.description}")
                    if finding.evidence:
                        lines.append(f"  Evidence: {', '.join(finding.evidence[:3])}")
                    if finding.recommendation:
                        lines.append(f"  Recommendation: {finding.recommendation}")
                    lines.append("")

            if sr.warnings:
                lines.append("#### Warnings")
                lines.append("")
                for warning in sr.warnings:
                    lines.append(f"- [WARNING] {warning}")
                lines.append("")

    return "\n".join(lines)


def write_markdown(report: AnalysisReport, path: str,
                   dump_categories: bool = True) -> None:
    """Write the Markdown report to *path*.

    Args:
        report: The analysed report.
        path: Destination of the Markdown file.
        dump_categories: Also write one file per selected coverage-loss
            category into a sidecar folder beside *path*, and link them from
            the triage section. Set False to write the Markdown alone.
    """
    dumps = None
    if dump_categories:
        from .category_dump import write_category_dumps

        try:
            dumps = write_category_dumps(report, path)
        except OSError as exc:
            # A read-only destination must not cost the user their report.
            logger.warning("Category dumps not written next to %s: %s",
                           path, exc)
            dumps = None
    content = render_markdown(report, dumps)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    logger.info("Markdown report written to %s", path)
