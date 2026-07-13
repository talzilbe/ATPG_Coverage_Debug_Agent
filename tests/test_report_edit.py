"""Tests for non-destructive report editing (exclude faults / annotate)."""

from __future__ import annotations

import os

from atpg_coverage_debug_agent.analysis import report_edit
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.reporting.session_report import (
    load_report,
    save_report,
)


def _rep(nl, fl, cn):
    return run_analysis(nl, fl, cn)


def test_exclude_class_recomputes_summary(sample_netlist_path,
                                          sample_faults_path,
                                          sample_constraints_path):
    rep = _rep(sample_netlist_path, sample_faults_path, sample_constraints_path)
    au_count = sum(1 for r in rep.fault_results
                   if r.fault.fault_class.value == "AU")
    edited = report_edit.apply_exclusions(
        rep, excluded_classes=["AU"], note="AU waived")
    assert all(r.fault.fault_class.value != "AU"
               for r in edited.fault_results)
    assert edited.summary.coverage_loss_count == \
        rep.summary.coverage_loss_count - au_count
    assert edited.summary.class_counts.get("AU", 0) == 0
    assert edited.edits["excluded_classes"] == ["AU"]
    assert edited.edits["note"] == "AU waived"
    # Base report is untouched (reversible).
    assert rep.summary.coverage_loss_count > 0
    assert any(r.fault.fault_class.value == "AU" for r in rep.fault_results) \
        or au_count == 0


def test_exclude_by_id(sample_netlist_path, sample_faults_path,
                       sample_constraints_path):
    rep = _rep(sample_netlist_path, sample_faults_path, sample_constraints_path)
    victim = rep.fault_results[0].fault.fault_object
    edited = report_edit.apply_exclusions(rep, excluded_ids=[victim])
    assert all(r.fault.fault_object != victim for r in edited.fault_results)
    assert edited.summary.coverage_loss_count == \
        rep.summary.coverage_loss_count - 1


def test_exclude_subtype_recomputes_summary(sample_netlist_path,
                                            sample_faults_path,
                                            sample_constraints_path):
    rep = _rep(sample_netlist_path, sample_faults_path, sample_constraints_path)
    au = [r for r in rep.fault_results if r.fault.fault_class.value == "AU"]
    assert au, "sample data should contain AU faults"
    # Tag one AU fault with a specific subtype and leave the rest as plain AU.
    au[0].fault.raw_class_token = "AU.NOFAULTS"
    edited = report_edit.apply_exclusions(
        rep, excluded_subtypes=["au.nofaults"], note="nofaults waived")
    victim = au[0].fault.fault_object
    assert all(r.fault.fault_object != victim for r in edited.fault_results)
    # Only the AU.NOFAULTS fault was removed; other AU faults remain.
    assert any(r.fault.fault_class.value == "AU"
               for r in edited.fault_results) or len(au) == 1
    assert edited.summary.coverage_loss_count == \
        rep.summary.coverage_loss_count - 1
    assert edited.edits["excluded_subtypes"] == ["AU.NOFAULTS"]


def test_exclude_combined_class_subtype_id(sample_netlist_path,
                                           sample_faults_path,
                                           sample_constraints_path):
    rep = _rep(sample_netlist_path, sample_faults_path, sample_constraints_path)
    uo = [r for r in rep.fault_results if r.fault.fault_class.value == "UO"]
    uc = [r for r in rep.fault_results if r.fault.fault_class.value == "UC"]
    assert uo and uc
    uo[0].fault.raw_class_token = "UO.AAB"
    victim_id = uc[0].fault.fault_object
    uc_count = len(uc)
    edited = report_edit.apply_exclusions(
        rep, excluded_classes=["UC"], excluded_subtypes=["UO.AAB"],
        excluded_ids=[victim_id])
    # UC removed by class (victim_id is a UC fault, counted once).
    assert all(r.fault.fault_class.value != "UC" for r in edited.fault_results)
    assert all(r.fault.fault_object != uo[0].fault.fault_object
               for r in edited.fault_results)
    assert edited.summary.coverage_loss_count == \
        rep.summary.coverage_loss_count - uc_count - 1


def test_edits_survive_save_load(tmp_path, sample_netlist_path,
                                 sample_faults_path, sample_constraints_path):
    rep = _rep(sample_netlist_path, sample_faults_path, sample_constraints_path)
    edited = report_edit.apply_exclusions(
        rep, excluded_classes=["AU"], note="waived")
    path = os.path.join(tmp_path, "edited.json")
    save_report(edited, path)
    loaded = load_report(path)
    assert loaded.edits["excluded_classes"] == ["AU"]
    assert loaded.edits["note"] == "waived"
    assert loaded.summary.coverage_loss_count == \
        edited.summary.coverage_loss_count


def test_edit_banner():
    banner = report_edit.edit_banner(
        {"excluded_classes": ["AU"], "excluded_ids": ["a/1"],
         "excluded_subtypes": ["AU.NOFAULTS"],
         "removed_count": 3, "note": "x"})
    assert "AU" in banner and "removed" in banner
    assert "AU.NOFAULTS" in banner
    assert report_edit.edit_banner(None) == ""
