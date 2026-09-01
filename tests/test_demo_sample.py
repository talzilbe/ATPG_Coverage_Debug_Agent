"""Tests for the shipped demo dataset and the HTML report's triage sections.

The demo files exist so the tool demonstrates itself on first run. If they stop
exercising the triage, the demo silently degrades to the fallback behaviour and
nobody notices — hence these assertions.
"""

from __future__ import annotations

import os

import pytest

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.reporting.html_report import build_html_report

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_HERE, "sample_data")


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(
        os.path.join(_SAMPLE, "demo_netlist.v"),
        os.path.join(_SAMPLE, "demo_faults.mtfi"),
        os.path.join(_SAMPLE, "demo_constraints.do"),
    )


# ---------------------------------------------------------------------------
# The demo dataset
# ---------------------------------------------------------------------------
def test_demo_files_are_present():
    for name in ("demo_netlist.v", "demo_faults.mtfi", "demo_constraints.do"):
        assert os.path.isfile(os.path.join(_SAMPLE, name)), name


def test_demo_exercises_several_dotted_subclasses(demo_report):
    present = {s.subclass_id for s in demo_report.statistics.loss_stats}
    assert {"AU.TC", "AU.PC", "UO.AAB", "AU.SEQ"} <= present


def test_demo_faults_map_onto_the_demo_netlist(demo_report):
    unresolved = sum(1 for r in demo_report.fault_results
                     if r.mapping.confidence.value == "unresolved")
    # A demo whose faults do not resolve would show none of the analysis.
    assert unresolved == 0


def test_demo_names_the_test_data_register_holding_the_tied_cone(demo_report):
    category = next(c for c in demo_report.selected_categories
                    if c.subclass_id == "AU.TC")
    assert category.attribution.verdict == "configurable_register"
    assert category.attribution.top_tie.driver == "uf_tdr_out_inter_reg"
    # The hardwired tie is also found, as the smaller contributor.
    assert any(s.driver == "uf_tie_lo" and s.kind == "tie_cell"
               for s in category.attribution.tie_sources)


def test_demo_names_the_constrained_pin_holding_the_io_cone(demo_report):
    category = next(c for c in demo_report.selected_categories
                    if c.subclass_id == "AU.PC")
    assert category.attribution.verdict == "user_configured"
    assert any(s.signal == "pi_hold"
               for s in category.attribution.constraint_sources)


def test_demo_profiles_the_aborted_category(demo_report):
    category = next(c for c in demo_report.selected_categories
                    if c.subclass_id == "UO.AAB")
    assert category.reachability.profiled > 0
    assert category.reachability.dominant


def test_demo_stuck_at_skew_is_visible_on_the_tied_category(demo_report):
    tc = demo_report.statistics.get("AU.TC")
    assert tc.sa_asymmetry == pytest.approx(1.0)


def test_demo_produces_a_fix_plan_ranked_by_fault_count(demo_report):
    counts = [r.fault_count for r in demo_report.recommendations]
    assert counts == sorted(counts, reverse=True)


def test_demo_promotes_the_topoff_for_the_traced_register(demo_report):
    # Within AU.TC, the traced test data register must promote the topoff
    # above the generic cheapest-first ordering.
    tc_fixes = [r for r in demo_report.recommendations
                if r.subclass_id == "AU.TC"]
    assert tc_fixes
    assert tc_fixes[0].fix.fix_id == "tc_tdr_topoff"
    assert any("uf_tdr_out_inter_reg" in line for line in tc_fixes[0].evidence)


def test_demo_promotes_waiving_for_the_user_configured_pins(demo_report):
    pc_fixes = [r for r in demo_report.recommendations
                if r.subclass_id == "AU.PC"]
    assert pc_fixes
    assert pc_fixes[0].fix.fix_id == "pc_waive_named"


def test_demo_report_passes_its_own_guardrails(demo_report):
    from atpg_coverage_debug_agent.analysis.guardrails import audit_report

    assert audit_report(demo_report) == []


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
def test_html_report_includes_the_triage_and_fix_plan(demo_report):
    html = build_html_report(demo_report, design_name="demo")
    assert "4. Coverage Triage" in html
    assert "5. Fix Plan" in html


def test_html_summary_reports_the_evidence_basis(demo_report):
    """The summary page must say how much of the loss rests on real evidence."""
    html = build_html_report(demo_report, design_name="demo")

    assert "3. Evidence Quality" in html
    assert "Coverage-loss faults by evidence basis" in html
    assert "Actionable coverage loss" in html
    # Scan status must be presented as read from pins, never guessed.
    assert "from the instantiation" in html
    assert "Not mapped" in html
    assert "unknown</b>, not zero" in html


def test_html_triage_names_the_blocking_structures(demo_report):
    html = build_html_report(demo_report, design_name="demo")
    assert "uf_tdr_out_inter_reg" in html
    assert "pi_hold" in html
    assert "What is blocking these faults" in html


def test_html_sections_are_numbered_without_gaps(demo_report):
    import re

    html = build_html_report(demo_report, design_name="demo")
    numbers = [int(m) for m in re.findall(r"<h2>(\d+)\.", html)]
    assert numbers == list(range(1, len(numbers) + 1))


def test_html_keeps_the_honesty_caveats(demo_report):
    html = build_html_report(demo_report, design_name="demo")
    assert "not</b> the ATPG tool&#x27;s test-coverage figure" in html \
        or "not</b> the ATPG tool's test-coverage figure" in html
    assert "no coverage gain is predicted" in html
    assert "It is not a root " in html


def test_html_report_without_triage_still_renders(demo_report):
    # A report saved before the triage existed must not break the renderer.
    demo_report.statistics = None
    demo_report.selected_categories = None
    demo_report.recommendations = None
    html = build_html_report(demo_report, design_name="demo")
    assert "4. Coverage Triage" in html
    assert "No coverage triage is stored" in html
