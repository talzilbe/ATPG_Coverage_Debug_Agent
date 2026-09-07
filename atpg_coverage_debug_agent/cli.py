"""Command-line interface for the ATPG coverage-loss debug agent.

Example::

    python -m atpg_coverage_debug_agent.cli \\
        --netlist path/to/netlist.v \\
        --faults path/to/faults.txt \\
        --constraints path/to/constraints.txt \\
        --report-md report.md \\
        --report-csv report.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .analysis.recommend import explain_category
from .app import run_analysis
from .models import AnalysisReport
from .reporting.csv_report import write_csv
from .reporting.markdown_report import write_markdown


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atpg_coverage_debug_agent",
        description="Debug ATPG/DFT coverage loss from a Verilog netlist, "
                    "a Tessent fault list and a constraint file.",
    )
    parser.add_argument("--netlist", default=None,
                        help="Path to the hierarchical Verilog netlist.")
    parser.add_argument("--faults", default=None,
                        help="Path to the Tessent ATPG fault list.")
    parser.add_argument("--constraints", default=None,
                        help="Path to the constraint file (optional).")
    parser.add_argument("--report-md", default=None,
                        help="Write a Markdown report to this path.")
    parser.add_argument("--report-csv", default=None,
                        help="Write a CSV report to this path.")
    parser.add_argument("--explain", metavar="SUBCLASS", default=None,
                        help="Explain a fault subclass (e.g. AU.TC) and the "
                             "fixes that apply, then exit. Needs no inputs.")
    parser.add_argument("--fix-limit", type=int, default=5,
                        help="How many fix proposals to print (default 5).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")
    return parser


def _print_explanation(subclass: str) -> int:
    """Print catalogue knowledge for *subclass*. Returns a process exit code."""
    data = explain_category(subclass)
    if not data.get("known"):
        print(f"'{data['subclass']}' is not in the catalogue, so nothing can "
              f"be stated about it.", file=sys.stderr)
        return 2

    print("=" * 60)
    print(f"{data['matched']} — {data['title']}")
    print("=" * 60)
    print(data["meaning"])
    if data["primary_causes"]:
        print("\nUsual causes:")
        for cause in data["primary_causes"]:
            print(f"  - {cause}")
    if data["evidence_needed"]:
        print("\nEvidence that would confirm it:")
        for item in data["evidence_needed"]:
            print(f"  - {item}")
    if data["caveat"]:
        print(f"\nCaveat: {data['caveat']}")
    print("\nCandidate fixes:")
    for fix in data["fixes"]:
        print(f"\n  * {fix['title']} [{fix['fix_id']}]")
        print(f"    Why:    {fix['rationale']}")
        if fix["expected_effect"]:
            print(f"    Effect: {fix['expected_effect']}")
        if fix["caveat"]:
            print(f"    Note:   {fix['caveat']}")
        for command in fix["commands"]:
            print(f"      {command}")
    print("=" * 60)
    return 0


def _print_triage(report: AnalysisReport, fix_limit: int) -> None:
    """Print the derived coverage triage and the ranked fix plan."""
    stats = report.statistics
    if stats is None:
        return

    print("\nCoverage triage (derived from the fault list):")
    print(f"  detected     : {stats.detected_count} "
          f"({stats.detected_pct:.2f}%)")
    print(f"  coverage loss: {stats.loss_count} ({stats.loss_pct:.2f}%)")

    loss_stats = stats.loss_stats
    if loss_stats:
        print("\n  Category      Faults        %   sa0/sa1   Imbalance")
        for st in loss_stats[:10]:
            print(f"  {st.subclass_id:<12s} {st.count:>6d} {st.pct:>7.2f}%   "
                  f"{st.sa0}/{st.sa1}   {st.sa_asymmetry:.2f}")

    selected = report.selected_categories or []
    hotspots = [c for c in selected if getattr(c, "clusters", None)
                and c.clusters.top]
    if hotspots:
        print("\n  Where the loss concentrates (where to look, not why):")
        for cat in hotspots:
            top = cat.clusters.top
            verdict = getattr(cat, "verdict", None)
            print(f"    {cat.subclass_id:<12s} {top.pct:>5.1f}% under "
                  f"{top.prefix}")
            if verdict is not None:
                print(f"      worth acting on: {verdict.actionable} "
                      f"({verdict.confidence.value}) "
                      f"[{', '.join(verdict.patterns) or 'no pattern'}]")

    blocked = [c for c in selected if getattr(c, "attribution", None)
               and c.attribution.attributed]
    if blocked:
        print("\n  What is blocking them (structural estimate, not the tool's "
              "own attribution):")
        for cat in blocked:
            att = cat.attribution
            print(f"    {cat.subclass_id:<12s} {att.verdict} "
                  f"({att.attributed}/{att.analysed} traced)")
            for src in att.tie_sources[:3]:
                value = f" tied {src.tie_value}" if src.tie_value else ""
                print(f"      {src.count:>6d}  {src.driver} "
                      f"[{src.kind}{value}]")
            for src in att.constraint_sources[:3]:
                print(f"      {src.count:>6d}  {src.signal} = "
                      f"{src.value or '?'} [{src.kind}]")

    profiled = [c for c in selected if getattr(c, "reachability", None)
                and c.reachability.profiled]
    if profiled:
        print("\n  Why they were hard to test (structural estimate):")
        for cat in profiled:
            prof = cat.reachability
            print(f"    {cat.subclass_id:<12s} {prof.dominant_label} "
                  f"({prof.dominant_share:.0%} of {prof.profiled} site(s))")

    recommendations = report.recommendations or []
    if not recommendations:
        return
    shown = recommendations[:max(1, fix_limit)]
    print(f"\nFix plan ({len(recommendations)} proposal(s), showing "
          f"{len(shown)}):")
    for rec in shown:
        print(f"\n  {rec.rank}. [{rec.subclass_id}] {rec.title}")
        print(f"     confidence={rec.confidence.value} "
              f"effort={rec.fix.effort} risk={rec.fix.risk}")
        print(f"     Why: {rec.fix.rationale}")
        if rec.fix.expected_effect:
            print(f"     Outcome: {rec.fix.expected_effect}")
        for caveat in rec.caveats:
            print(f"     Caveat: {caveat}")
        for command in rec.fix.commands:
            print(f"       {command}")


def _print_summary(report: AnalysisReport) -> None:
    s = report.summary
    print("=" * 60)
    print("ATPG COVERAGE-LOSS SUMMARY")
    print("=" * 60)
    print(f"Total faults analysed : {s.total_faults}")
    print(f"Coverage-loss faults  : {s.coverage_loss_count}")
    if s.coverage_loss_count:
        print("\nEvidence basis of the coverage loss:")
        print(f"  mapped onto netlist : {s.mapped_count}")
        print(f"  NOT mapped          : {s.unmapped_count} "
              f"(connectivity unknown, not zero)")
        print(f"  tied to a constant  : {s.tied_constant_count} "
              f"(expected, non-actionable)")
        print(f"  actionable loss     : {s.actionable_loss_count}")
        scan = dict(s.scan_evidence_counts or {})
        if scan:
            parts = ", ".join(f"{k}={scan[k]}" for k in
                              ("scan", "non_scan", "unknown") if k in scan)
            print(f"  scan status (pins)  : {parts}")
        causes = dict(s.unresolved_causes or {})
        if causes:
            parts = ", ".join(f"{k}={v}" for k, v in
                              sorted(causes.items(), key=lambda kv: -kv[1]))
            print(f"  unmapped because    : {parts}")
    print("\nFault class counts (complete — every class present; the dotted "
          "breakdown by coverage role is in the census below):")
    # Every class present, largest first. A fixed list silently omits any
    # class the analyzer was not written to expect.
    for cls, count in sorted(s.class_counts.items(),
                             key=lambda kv: (-kv[1], kv[0])):
        print(f"  {cls:10s}: {count}")
    _print_metrics(report)
    _print_census(report)
    _print_input_quality(report)
    print("\nTop root causes:")
    for name, count in s.top_root_causes:
        print(f"  {count:4d}  {name}")
    print("\nTop affected instances (actionable loss only):")
    for name, count in s.top_instances[:5]:
        print(f"  {count:4d}  {name}")
    if report.warnings:
        print(f"\nWarnings ({len(report.warnings)}):")
        for w in report.warnings[:10]:
            print(f"  - {w}")
        if len(report.warnings) > 10:
            print(f"  ... and {len(report.warnings) - 10} more.")
    print("=" * 60)


def _print_metrics(report: AnalysisReport) -> None:
    """Print the three Tessent coverage metrics with their inputs."""
    stats = getattr(report, "statistics", None)
    if stats is None or not hasattr(stats, "metrics"):
        return
    m = stats.metrics()
    roles = m["roles"]

    def _val(key: str) -> str:
        value = m[key]
        return "n/a" if value is None else f"{value:.4f}%"

    print("\nCoverage metrics (posdet_credit="
          f"{m['posdet_credit']}, numerator={m['detected_credit']}):")
    formulas = m.get("formulas", {})
    for key, label in (("test_coverage", "test coverage      "),
                       ("fault_coverage", "fault coverage     "),
                       ("atpg_effectiveness", "atpg effectiveness ")):
        spec = formulas.get(key, {})
        print(f"  {label} : {_val(key)}   {spec.get('formula', '')}")
        if spec.get("substitution"):
            print(f"                        {spec['substitution']}")
    print("  roles               : "
          + ", ".join(f"{r}={roles.get(r, 0)}" for r in
                      ("DT", "PD", "UD", "AU", "ND"))
          + f", FU={m['total_faults']}")
    print(f"  UD is              : {m.get('ud_definition', '')}")
    if m["unrecognised"]:
        print(f"  unrecognised class  : {m['unrecognised']} "
              f"(excluded from every metric)")
    if not m["census_balances"]:
        print("  !! census does not reconcile with the parsed record count; "
              "the figures above are unusable")


def _print_census(report: AnalysisReport) -> None:
    """Print the complete fault census (S2b) with its self-check.

    Every class present, grouped by coverage role, followed by the sum check.
    Printed on every run: a partial listing that a reader has to reconcile by
    hand is how a residual turns into an invented fault category.
    """
    from .analysis.census import build_census

    census = build_census(report)
    if not census.entries:
        return
    print(f"\nComplete fault census (S2b) — {census.class_count} class(es) "
          f"across {len(census.roles)} coverage role(s):")
    for role in census.roles:
        print(f"  {role.role:<13s} {role.label:<20s} {role.count:>10d}")
        for family in role.families:
            for entry in family.entries:
                print(f"      {entry.subclass:<16s} {entry.count:>10d}  "
                      f"{entry.pct:6.2f}%  sa0={entry.sa0} sa1={entry.sa1}")
    rec = census.reconciliation()
    if rec["reconciles"]:
        print(f"  self-check: {rec['sum_of_class_counts']} counted == "
              f"{rec['total_faults']} analysed (delta 0).")
    else:
        print(f"  !! SELF-CHECK FAILED: {rec['sum_of_class_counts']} counted "
              f"vs {rec['total_faults']} analysed, delta {rec['delta']}. "
              f"Do not attribute the delta to a category.")
    if rec["unclassified_tokens"]:
        print(f"  !! unclassified class token(s): "
              f"{', '.join(rec['unclassified_tokens'])} — not in the "
              f"class-role map, so they feed no coverage metric.")


def _print_input_quality(report: AnalysisReport) -> None:
    """Print how completely the inputs were understood."""
    header = getattr(report, "fault_list_header", None)
    if header is not None:
        print("\nFault list as declared:")
        print(f"  format / compression: {header.file_format} / "
              f"{header.compression}")
        print(f"  fault collapsing    : {header.collapsing_label}")
        print(f"  fault model(s)      : "
              f"{', '.join(header.fault_models) or 'not declared'}")
        print(f"  declared columns    : "
              f"{', '.join(header.format_fields) or 'not declared'}")

    diagnostics = getattr(report, "class_diagnostics", None)
    if diagnostics is not None and getattr(diagnostics, "tokens", None):
        print(f"\nUnrecognised fault classes "
              f"({diagnostics.count}/{diagnostics.total} record(s), "
              f"{diagnostics.pct:.4f}%):")
        for tok in diagnostics.tokens:
            sample = tok.samples[0] if tok.samples else ""
            print(f"  {tok.token:12s} x{tok.count}  first line "
                  f"{tok.first_line or '?'}  e.g. {sample}")

    status = getattr(report, "constraint_diagnostics", None)
    if status:
        unresolved = status.get("unresolved", 0)
        print("\nConstraint file parsing:")
        print(f"  files read          : {len(status.get('files') or [])}")
        print(f"  directives evaluated: {status.get('resolved', 0)} of "
              f"{status.get('directives', 0)}")
        print(f"  NOT evaluated       : {unresolved}")
        print(f"  objects expanded    : {status.get('expanded_objects', 0)}")
        if unresolved:
            print("  !! a fault with no constraint hit is NOT proven "
                  "unconstrained: the file was only partly understood")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.explain:
        return _print_explanation(args.explain)

    missing = [name for name, value in
               (("--netlist", args.netlist), ("--faults", args.faults))
               if not value]
    if missing:
        print(f"ERROR: {' and '.join(missing)} required for analysis.",
              file=sys.stderr)
        return 2

    try:
        report = run_analysis(args.netlist, args.faults, args.constraints)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - unexpected fatal error
        print(f"FATAL: unexpected error during analysis: {exc}",
              file=sys.stderr)
        return 1

    _print_summary(report)
    _print_triage(report, args.fix_limit)

    if args.report_md:
        write_markdown(report, args.report_md)
        print(f"Markdown report: {args.report_md}")
    if args.report_csv:
        write_csv(report, args.report_csv)
        print(f"CSV report: {args.report_csv}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
