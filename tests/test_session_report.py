"""Tests for saving and loading a full AnalysisReport as JSON."""

from __future__ import annotations

import os

import pytest

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.reporting.session_report import (
    load_report,
    report_to_dict,
    save_report,
    dict_to_report,
)


def test_report_roundtrip_preserves_data(tmp_path, sample_netlist_path,
                                         sample_faults_path,
                                         sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    path = os.path.join(tmp_path, "r.json")
    save_report(rep, path)
    assert os.path.isfile(path)

    loaded = load_report(path)
    assert loaded.summary.total_faults == rep.summary.total_faults
    assert loaded.summary.coverage_loss_count == rep.summary.coverage_loss_count
    assert len(loaded.fault_results) == len(rep.fault_results)
    assert len(loaded.constraints) == len(rep.constraints)

    # Field-level fidelity on the first fault result.
    a, b = rep.fault_results[0], loaded.fault_results[0]
    assert a.fault.fault_object == b.fault.fault_object
    assert a.fault.fault_class == b.fault.fault_class
    assert a.mapping.confidence == b.mapping.confidence
    assert a.root_cause == b.root_cause
    assert a.evidence == b.evidence
    assert a.recommended_step == b.recommended_step


def test_loaded_report_supports_queries(tmp_path, sample_netlist_path,
                                        sample_faults_path,
                                        sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    path = os.path.join(tmp_path, "r.json")
    save_report(rep, path)
    loaded = load_report(path)

    # The agent's query core must work on a reloaded report.
    live = investigate.list_faults(rep.fault_results)["total_matched"]
    reload_total = investigate.list_faults(loaded.fault_results)["total_matched"]
    assert live == reload_total
    # Adjacency was preserved for path tracing without the live netlist.
    assert isinstance(loaded.adjacency, dict)
    assert loaded.netlist is None


def test_load_rejects_foreign_json(tmp_path):
    bad = os.path.join(tmp_path, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write('{"hello": "world"}')
    with pytest.raises(ValueError):
        load_report(bad)


def test_sources_metadata_roundtrip(tmp_path, sample_netlist_path,
                                    sample_faults_path,
                                    sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    assert rep.sources and rep.sources.get("netlist") == sample_netlist_path
    path = os.path.join(tmp_path, "r.json")
    save_report(rep, path)
    loaded = load_report(path)
    assert loaded.sources.get("netlist") == sample_netlist_path
    assert loaded.sources.get("faults") == sample_faults_path
    assert loaded.sources.get("design")  # design name derived from netlist


def test_export_evidence_uses_adjacency_fallback(sample_netlist_path,
                                                 sample_faults_path,
                                                 sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    data = report_to_dict(rep)
    loaded = dict_to_report(data)
    # With no live netlist, export uses the stored adjacency.
    ev = investigate.export_evidence(
        loaded.fault_results, loaded.constraints, loaded.netlist,
        adjacency=loaded.adjacency)
    assert ev["adjacency"] == loaded.adjacency
