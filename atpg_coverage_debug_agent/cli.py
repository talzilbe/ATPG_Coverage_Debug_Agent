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
    print("\nFault class counts:")
    for cls in ("DS", "DI", "TI", "AU", "UO", "UC", "UNKNOWN"):
        if cls in s.class_counts:
            print(f"  {cls:8s}: {s.class_counts[cls]}")
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
