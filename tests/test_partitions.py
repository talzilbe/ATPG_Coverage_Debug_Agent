"""Tests for multi-partition analysis orchestration."""

from __future__ import annotations

from atpg_coverage_debug_agent.app import (
    AnalysisInputs,
    PartitionInputs,
    analyze_partitions,
)


def test_analyze_partitions_returns_all(sample_netlist_path, sample_faults_path,
                                        sample_constraints_path):
    parts = [
        PartitionInputs(
            "p1",
            AnalysisInputs(sample_netlist_path, sample_faults_path,
                           sample_constraints_path)),
        PartitionInputs(
            "p2",
            AnalysisInputs(sample_netlist_path, sample_faults_path, None)),
    ]
    results = analyze_partitions(parts)
    assert [name for name, _ in results] == ["p1", "p2"]
    assert all(rep.summary.total_faults > 0 for _, rep in results)
    # Independent report objects so each tab/selector shows its own data.
    assert results[0][1] is not results[1][1]


def test_analyze_partitions_progress(sample_netlist_path, sample_faults_path):
    seen = []
    parts = [
        PartitionInputs(
            "alpha",
            AnalysisInputs(sample_netlist_path, sample_faults_path, None)),
    ]
    analyze_partitions(parts, progress=lambda d, t, m: seen.append((d, t, m)))
    assert seen, "progress callback should be invoked"
    assert seen[-1][0] == seen[-1][1]  # final call reports done == total
    assert any("alpha" in m for _, _, m in seen)


def test_analyze_partitions_empty():
    assert analyze_partitions([]) == []
