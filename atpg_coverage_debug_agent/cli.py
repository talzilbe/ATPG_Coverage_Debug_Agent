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
    parser.add_argument("--netlist", required=True,
                        help="Path to the hierarchical Verilog netlist.")
    parser.add_argument("--faults", required=True,
                        help="Path to the Tessent ATPG fault list.")
    parser.add_argument("--constraints", default=None,
                        help="Path to the constraint file (optional).")
    parser.add_argument("--report-md", default=None,
                        help="Write a Markdown report to this path.")
    parser.add_argument("--report-csv", default=None,
                        help="Write a CSV report to this path.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging.")
    return parser


def _print_summary(report: AnalysisReport) -> None:
    s = report.summary
    print("=" * 60)
    print("ATPG COVERAGE-LOSS SUMMARY")
    print("=" * 60)
    print(f"Total faults analysed : {s.total_faults}")
    print(f"Coverage-loss faults  : {s.coverage_loss_count}")
    print("\nFault class counts:")
    for cls in ("DS", "DI", "TI", "AU", "UO", "UC", "UNKNOWN"):
        if cls in s.class_counts:
            print(f"  {cls:8s}: {s.class_counts[cls]}")
    print("\nTop root causes:")
    for name, count in s.top_root_causes:
        print(f"  {count:4d}  {name}")
    print("\nTop affected instances:")
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

    if args.report_md:
        write_markdown(report, args.report_md)
        print(f"Markdown report: {args.report_md}")
    if args.report_csv:
        write_csv(report, args.report_csv)
        print(f"CSV report: {args.report_csv}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
