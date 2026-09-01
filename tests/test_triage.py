"""Tests for the subclass taxonomy, derived statistics and fix catalogue."""

from __future__ import annotations

import pytest

from atpg_coverage_debug_agent.analysis.recommend import (
    build_recommendations,
    explain_category,
)
from atpg_coverage_debug_agent.analysis.statistics import (
    compute_statistics,
    select_categories,
)
from atpg_coverage_debug_agent.knowledge.fixes import (
    FIX_CATALOG,
    MAX_ABORT_LIMIT,
    fixes_for_subclass,
)
from atpg_coverage_debug_agent.knowledge.subclasses import (
    SUBCLASS_CATALOG,
    describe_subclass,
    is_coverage_loss_class,
)
from atpg_coverage_debug_agent.models import FaultClass, VerdictConfidence
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list


def _fault(dotted: str, stuck: str, path: str):
    """Build one fault record through the parser, as production code would."""
    records, _ = parse_fault_list(f"{dotted} {stuck} {path}")
    assert records, f"parser produced no record for {dotted!r}"
    return records[0]


def _faults(spec):
    """Build a fault list from ``(dotted, stuck, count)`` triples."""
    out = []
    for index, (dotted, stuck, count) in enumerate(spec):
        for n in range(count):
            out.append(_fault(dotted, stuck, f"top/blk{index}/u{n}/Y"))
    return out


# ---------------------------------------------------------------------------
# Subclass parsing
# ---------------------------------------------------------------------------
def test_dotted_class_token_is_split_into_class_and_subclass():
    fault = _fault("AU.TC", "1", "top/u_seq/optlc_900/o")
    assert fault.fault_class is FaultClass.AU
    assert fault.subclass == "TC"
    assert fault.dotted_class == "AU.TC"
    assert fault.sa_key == "sa1"


def test_bare_class_token_has_no_subclass():
    fault = _fault("UO", "0", "top/u_alu/U2/A")
    assert fault.subclass is None
    assert fault.dotted_class == "UO"
    assert fault.sa_key == "sa0"


def test_mtfi_dotted_classes_are_preserved():
    text = """FaultInformation {
     FaultType (Stuck) {
      FaultList {
       Format : Identifier, Class, Location;
       Instance ("") {
          0,  AU.TC,     "/top/u_seq/optlc_900/o";
          1,  UO.AAB,    "/top/u_ctrl/reg/q";
          0,  DS,        "/top/u_xyz/o";
"""
    records, _ = parse_fault_list(text)
    dotted = [r.dotted_class for r in records]
    assert dotted == ["AU.TC", "UO.AAB", "DS"]


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
def test_exact_subclass_lookup_beats_family_fallback():
    assert describe_subclass("AU.TC").subclass_id == "AU.TC"
    # An uncatalogued subtype still resolves via its family.
    assert describe_subclass("AU.WHATEVER").subclass_id == "AU"
    assert describe_subclass("ZZ.QQ") is None


@pytest.mark.parametrize("dotted,expected", [
    ("AU.TC", True),
    ("UO.AAB", True),
    ("UC", True),
    ("DS", False),
    ("DI.CLK", False),
    ("TI", False),
    ("UNKNOWN", False),
    ("", False),
])
def test_only_loss_classes_are_debug_targets(dotted, expected):
    assert is_coverage_loss_class(dotted) is expected


def test_every_catalogued_fix_id_resolves():
    for info in SUBCLASS_CATALOG.values():
        for fix_id in info.fix_ids:
            assert fix_id in FIX_CATALOG, (
                f"{info.subclass_id} references unknown fix {fix_id!r}")


def test_unknown_subclass_falls_back_to_generic_review():
    actions = fixes_for_subclass("ZZ.QQ")
    assert [a.fix_id for a in actions] == ["generic_review"]


def test_abort_limit_guidance_never_exceeds_the_cap():
    for action in FIX_CATALOG.values():
        for command in action.commands:
            if "abort_limit" in command and not command.startswith("#"):
                value = int(command.rsplit(" ", 1)[-1])
                assert value <= MAX_ABORT_LIMIT


# ---------------------------------------------------------------------------
# Derived statistics
# ---------------------------------------------------------------------------
def test_statistics_aggregate_counts_percentages_and_stuck_split():
    faults = _faults([
        ("DS", "0", 60),
        ("AU.TC", "1", 30),
        ("UO.AAB", "0", 10),
    ])
    stats = compute_statistics(faults)

    assert stats.total_faults == 100
    assert stats.detected_count == 60
    assert stats.loss_count == 40
    assert stats.detected_pct == pytest.approx(60.0)

    tc = stats.get("AU.TC")
    assert (tc.count, tc.sa1, tc.sa0) == (30, 30, 0)
    assert tc.pct == pytest.approx(30.0)
    # Every fault is stuck-at-1, so the polarities are maximally imbalanced.
    assert tc.sa_asymmetry == pytest.approx(1.0)


def test_statistics_of_empty_fault_list_are_zeroed_not_an_error():
    stats = compute_statistics([])
    assert stats.total_faults == 0
    assert stats.detected_pct == 0.0
    assert stats.loss_stats == []
    assert select_categories(stats) == []


def test_detected_classes_are_never_selected_for_debug():
    faults = _faults([("DS", "0", 90), ("AU.PC", "1", 10)])
    selected = select_categories(compute_statistics(faults))
    assert [c.subclass_id for c in selected] == ["AU.PC"]


def test_selection_drops_sparse_tail_and_caps_the_list():
    faults = _faults([
        ("AU.PC", "0", 500),
        ("AU.TC", "1", 300),
        ("UO.AAB", "0", 100),
        ("AU.SEQ", "1", 60),
        ("AU.BB", "0", 30),
        ("AU.UDN", "1", 8),      # 0.8% -> below the 1% threshold
    ])
    selected = select_categories(compute_statistics(faults), max_categories=3)
    assert [c.subclass_id for c in selected] == ["AU.PC", "AU.TC", "UO.AAB"]
    assert all(c.stat.pct >= 1.0 for c in selected)


def test_selection_falls_back_when_loss_is_spread_too_thinly():
    # 200 distinct categories of one fault each: nothing reaches 1%.
    faults = _faults([(f"AU.S{i}", "0", 1) for i in range(200)])
    stats = compute_statistics(faults)
    selected = select_categories(stats, fallback_top_n=3)
    assert len(selected) == 3, "a fragmented design must still yield candidates"
    assert "spread thinly" in selected[0].reason


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------
def test_recommendations_are_ranked_by_fault_count():
    faults = _faults([("AU.TC", "1", 300), ("UO.AAB", "0", 100)])
    stats = compute_statistics(faults)
    recs = build_recommendations(stats)

    assert recs
    assert recs[0].subclass_id == "AU.TC"
    counts = [r.fault_count for r in recs]
    assert counts == sorted(counts, reverse=True)


def test_cheaper_actions_come_first_within_a_category():
    stats = compute_statistics(_faults([("AU.TC", "1", 100)]))
    recs = build_recommendations(stats)
    ranks = [r.fix.feasibility_rank for r in recs]
    assert ranks == sorted(ranks)


def test_subtyped_faults_score_higher_confidence_than_bare_ones():
    typed = build_recommendations(
        compute_statistics(_faults([("AU.TC", "1", 100)])))
    bare = build_recommendations(
        compute_statistics(_faults([("AU", "1", 100)])))
    assert typed[0].confidence is VerdictConfidence.HIGH
    assert bare[0].confidence is VerdictConfidence.REDUCED


def test_evidence_is_tagged_with_its_source():
    stats = compute_statistics(_faults([("AU.TC", "1", 100)]))
    rec = build_recommendations(stats)[0]
    assert rec.evidence
    assert all(line.startswith("[") for line in rec.evidence)
    assert any("[fault_list]" in line for line in rec.evidence)


def test_measured_actions_carry_a_no_prediction_caveat():
    stats = compute_statistics(_faults([("AU.TC", "1", 100)]))
    measured = [r for r in build_recommendations(stats)
                if r.requires_measurement]
    assert measured
    for rec in measured:
        assert any("only be established by re-running" in c
                   for c in rec.caveats)


def test_no_coverage_loss_yields_no_recommendations():
    stats = compute_statistics(_faults([("DS", "0", 50)]))
    assert build_recommendations(stats) == []


def test_explain_category_reports_unknown_classes_honestly():
    known = explain_category("AU.TC")
    assert known["known"] is True
    assert known["matched"] == "AU.TC"
    assert known["fixes"]

    unknown = explain_category("ZZ.QQ")
    assert unknown["known"] is False
    assert "fixes" not in unknown


# ---------------------------------------------------------------------------
# Integration with the surrounding pipeline
# ---------------------------------------------------------------------------
def test_analysis_pipeline_attaches_triage_to_the_report(
        sample_netlist_path, sample_faults_path, sample_constraints_path):
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path,
                          sample_constraints_path)
    assert report.statistics is not None
    assert report.statistics.total_faults == report.summary.total_faults
    assert report.selected_categories
    assert report.recommendations


def test_markdown_report_includes_the_triage_and_fix_plan(
        sample_netlist_path, sample_faults_path, sample_constraints_path):
    from atpg_coverage_debug_agent.app import run_analysis
    from atpg_coverage_debug_agent.reporting.markdown_report import (
        render_markdown,
    )

    text = render_markdown(run_analysis(
        sample_netlist_path, sample_faults_path, sample_constraints_path))
    assert "## Coverage Triage" in text
    assert "## Fix Plan" in text
    assert "not the tool's test-coverage number" in text


def test_saved_session_restores_the_triage(
        tmp_path, sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.app import run_analysis
    from atpg_coverage_debug_agent.reporting.session_report import (
        load_report,
        save_report,
    )

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    path = str(tmp_path / "session.json")
    save_report(report, path)
    reloaded = load_report(path)

    assert reloaded.statistics is not None
    assert reloaded.statistics.total_faults == report.statistics.total_faults
    assert ([c.subclass_id for c in reloaded.selected_categories]
            == [c.subclass_id for c in report.selected_categories])


def test_waived_faults_leave_the_triage(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.analysis.report_edit import apply_exclusions
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    assert report.statistics.get("AU") is not None

    edited = apply_exclusions(report, excluded_classes=["AU"])
    assert edited.statistics.get("AU") is None
    assert all(c.subclass_id != "AU" for c in edited.selected_categories)
    assert all(r.subclass_id != "AU" for r in edited.recommendations)


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------
def _run(name, args=None, triage=None):
    from atpg_coverage_debug_agent.analysis import investigate

    return investigate.run_tool(
        name, args or {}, fault_results=[], constraints=[], netlist=None,
        triage=triage)


def test_explain_subclass_tool_needs_no_analysis_loaded():
    data = _run("explain_subclass", {"subclass": "UO.AAB"})
    assert data["known"] is True
    assert data["matched"] == "UO.AAB"


def test_triage_tools_say_so_when_no_analysis_is_loaded():
    for name in ("coverage_triage", "recommend_fixes"):
        assert "error" in _run(name)


def test_triage_tools_read_the_serialized_payload(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.analysis import investigate
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    payload = investigate.serialize_triage(
        report.statistics, report.selected_categories, report.recommendations)

    triage = _run("coverage_triage", triage=payload)
    assert triage["totals"]["coverage_loss"] == report.statistics.loss_count
    assert triage["selected"]

    fixes = _run("recommend_fixes", {"limit": 2}, triage=payload)
    assert fixes["returned"] == 2
    assert fixes["recommendations"][0]["commands"] is not None

    filtered = _run("recommend_fixes", {"subclass": "AU"}, triage=payload)
    assert all(r["subclass"] == "AU"
               for r in filtered["recommendations"])
