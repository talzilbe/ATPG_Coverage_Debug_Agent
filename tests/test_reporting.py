"""End-to-end tests for analysis orchestration and report generation."""

from __future__ import annotations

import os

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.reporting.csv_report import (
    render_rows,
    write_csv,
)
from atpg_coverage_debug_agent.reporting.markdown_report import (
    render_markdown,
    write_markdown,
)


def test_full_pipeline(sample_netlist_path, sample_faults_path,
                       sample_constraints_path):
    report = run_analysis(sample_netlist_path, sample_faults_path,
                          sample_constraints_path)
    assert report.summary.total_faults > 0
    assert report.summary.coverage_loss_count > 0
    # Every coverage-loss fault should have a result with evidence.
    for r in report.fault_results:
        assert r.evidence
        assert r.recommended_step


def test_runs_without_constraints(sample_netlist_path, sample_faults_path):
    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    assert any("No constraints" in w for w in report.warnings)


def test_markdown_report(tmp_path, sample_netlist_path, sample_faults_path,
                         sample_constraints_path):
    report = run_analysis(sample_netlist_path, sample_faults_path,
                          sample_constraints_path)
    md = render_markdown(report)
    assert "ATPG Coverage-Loss Debug Report" in md
    out = tmp_path / "report.md"
    write_markdown(report, str(out))
    assert out.exists() and out.stat().st_size > 0


def test_csv_report(tmp_path, sample_netlist_path, sample_faults_path,
                    sample_constraints_path):
    report = run_analysis(sample_netlist_path, sample_faults_path,
                          sample_constraints_path)
    rows = render_rows(report)
    assert rows
    assert "root_cause" in rows[0]
    out = tmp_path / "report.csv"
    write_csv(report, str(out))
    assert out.exists() and out.stat().st_size > 0
